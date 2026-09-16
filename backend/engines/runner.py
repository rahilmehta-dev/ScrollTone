"""Launches a chapter's synthesis on an alternate TTS engine (Higgs Audio V2 or
Chatterbox) as a subprocess in that engine's own venv, and collects the result.
Also used for Kokoro, but only when running with multiple Parallel Workers —
see the "kokoro" entry below and chapter_processor.py's _narrate().

Kept dependency-light (stdlib + numpy/soundfile, both already base deps) so it
can be imported by the main app process, which does NOT have transformers or
chatterbox-tts installed — those live only in .venv-higgs / .venv-chatterbox.
See documentation/engines.md for setup.
"""
import json
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np
import soundfile as sf

import backend.state as state

PROGRESS_RE = re.compile(r"^##PROGRESS##\s+(\d+)\s+(\d+)\s*$")
# See backend/engines/_heartbeat.py — higgs_synth.py/chatterbox_synth.py print
# these periodically during a long silent stretch (model load, one generate()
# call) so it doesn't look like a hang; matched lines get forwarded to the
# job log via on_log instead of being discarded like other worker stdout.
HEARTBEAT_RE = re.compile(r"^##HEARTBEAT##\s+(.*)$")

ENGINE_CONFIG = {
    "higgs": {
        "venv": state.BASE_DIR / ".venv-higgs",
        "script": Path(__file__).parent / "higgs_synth.py",
        "setup_hint": (
            "python -m venv .venv-higgs && "
            ".venv-higgs/bin/pip install transformers torch torchaudio "
            "accelerate librosa soundfile"
        ),
    },
    "chatterbox": {
        "venv": state.BASE_DIR / ".venv-chatterbox",
        "script": Path(__file__).parent / "chatterbox_synth.py",
        "setup_hint": (
            "python -m venv .venv-chatterbox && "
            ".venv-chatterbox/bin/pip install chatterbox-tts torch torchaudio"
        ),
    },
    # No separate venv — Kokoro's deps already live in the main app venv.
    # venv_python() below special-cases this to the current interpreter.
    "kokoro": {
        "venv": None,
        "script": Path(__file__).parent / "kokoro_synth.py",
        "setup_hint": "",
    },
}


class EngineNotInstalled(RuntimeError):
    pass


def venv_python(engine: str) -> Path:
    if engine == "kokoro":
        return Path(sys.executable)
    cfg = ENGINE_CONFIG[engine]
    py = cfg["venv"] / "bin" / "python"
    if not py.exists():
        raise EngineNotInstalled(
            f"{engine} engine is not set up on this machine — expected {cfg['venv']}. "
            f"Set it up with:\n  {cfg['setup_hint']}\n"
            f"See documentation/engines.md."
        )
    return py


def _split_indices(n: int, num_workers: int) -> list[list[int]]:
    """Split range(n) into up to num_workers contiguous, near-equal slices."""
    num_workers = max(1, min(num_workers, n)) if n else 0
    base, extra = divmod(n, num_workers) if num_workers else (0, 0)
    slices, start = [], 0
    for worker in range(num_workers):
        size = base + (1 if worker < extra else 0)
        if size:
            slices.append(list(range(start, start + size)))
            start += size
    return slices


def _safe_call(callback, *args, what: str) -> None:
    """Invoke a caller-supplied callback, swallowing anything it raises.

    These run on the per-worker stdout reader threads in
    _synthesize_chapter_parallel(). An exception propagating out of one
    kills that thread, which is the only thing draining that worker's
    stdout pipe — the worker then blocks forever on a full pipe and the
    proc.wait() below never returns, hanging the whole job with no error
    reported anywhere. A callback failing (e.g. voice_tuning.py's DNSMOS
    scoring choking on one bad wav) must cost at most that one callback.
    """
    if callback is None:
        return
    try:
        callback(*args)
    except Exception as error:  # noqa: BLE001
        print(f"[runner] {what} callback failed: {type(error).__name__}: {error}",
              file=sys.stderr, flush=True)


def synthesize_chapter(
    engine: str,
    chunks: list[str],
    reference_wav: str,
    device: str,
    on_progress: Callable[[int, int], None],
    stop_check: Callable[[], bool] | None = None,
    extra_config: dict | None = None,
    num_workers: int = 1,
    on_log: Callable[[str], None] | None = None,
    on_chunk_done: Callable[[int, Path], None] | None = None,
) -> tuple[list[np.ndarray], dict]:
    """Run one chapter's chunks through an alternate engine's subprocess.

    `extra_config` is merged into the worker's job.json as extra top-level
    keys — e.g. {"temperature": 0.6} to override higgs_synth.py's default
    sampling temperature. Unrecognized keys are ignored by the worker script,
    so this is safe to pass even for a worker that doesn't use them.

    `extra_config["per_chunk"]`, if present, is a list[dict] the same length
    as `chunks` — one params override per chunk instead of one shared config
    for the whole job. Used by backend/voice_tuning.py's auto-tune job, which
    synthesizes the same sample text N times with N different LHS-sampled
    parameter sets to score against each other. Handled specially (sliced
    per worker, not spread like other extra_config keys) and passed to the
    worker as `chunk_params` in job.json.

    `num_workers` > 1 fans the chunk list out across that many concurrent
    subprocesses (each loading its own model instance — RAM scales roughly
    linearly with worker count) instead of one subprocess handling every
    chunk sequentially. Only worth it on CPU-only engines with cores/RAM to
    spare; see chatterbox_synth.py's module docstring for the RAM profile of
    a single worker.

    `on_log`, if given, receives one string per heartbeat tick a worker
    prints during a long silent stretch (model load, a single generate()
    call) — see backend/engines/_heartbeat.py. Optional since Kokoro's
    in-process path and short-chunk engines don't need it.

    `on_chunk_done`, if given, fires as soon as each individual chunk's wav
    file is written — (global_chunk_index, path_to_wav) — rather than
    waiting for every chunk in the job to finish. The worker scripts write
    each chunk's file *before* printing its progress line, so the file is
    guaranteed to exist by the time this fires. The path points at a
    temporary location that's deleted once this whole call returns (it's
    inside the `with tempfile.TemporaryDirectory()` block below/in the
    parallel path) — the callback must read or copy it immediately if it
    needs the data past that. Used by backend/voice_tuning.py's auto-tune
    job to score each candidate as it finishes instead of only after the
    whole batch completes.

    Returns (audio_arrays_in_order, results_dict). audio_arrays may be shorter
    than `chunks` if some failed or a safety abort truncated the run — check
    results_dict["errors"] / ["safety_abort"] for details.
    """
    if num_workers > 1 and len(chunks) > 1:
        return _synthesize_chapter_parallel(
            engine, chunks, reference_wav, device, on_progress, stop_check,
            extra_config, num_workers, on_log, on_chunk_done,
        )

    py = venv_python(engine)
    script = ENGINE_CONFIG[engine]["script"]
    # "per_chunk" is a reserved extra_config key (list[dict], one per chunk) —
    # e.g. voice_tuning.py's auto-tune job uses it to give each "chunk" (the
    # same sample text, repeated) its own sampling params instead of one
    # config shared by every chunk. Pulled out here instead of being spread
    # like other extra_config keys, since the worker scripts read it as
    # `chunk_params` (per-chunk), not a single top-level scalar.
    cfg = dict(extra_config or {})
    per_chunk = cfg.pop("per_chunk", None)

    with tempfile.TemporaryDirectory(prefix=f"scrolltone_{engine}_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        job_path = tmp_path / "job.json"
        out_dir = tmp_path / "out"
        job_path.write_text(json.dumps({
            "chunks": chunks,
            "reference_wav": reference_wav,
            "out_dir": str(out_dir),
            "device": device,
            **cfg,
            **({"chunk_params": per_chunk} if per_chunk is not None else {}),
        }))

        stderr_path = tmp_path / "stderr.log"
        with open(stderr_path, "w") as stderr_file:
            proc = subprocess.Popen(
                [str(py), str(script), str(job_path)],
                stdout=subprocess.PIPE, stderr=stderr_file,
                text=True, bufsize=1,
            )
            stopped = False
            for line in proc.stdout:
                match = PROGRESS_RE.match(line)
                if match:
                    done_n = int(match.group(1))
                    _safe_call(on_progress, done_n, int(match.group(2)), what="on_progress")
                    if on_chunk_done is not None:
                        # No chunk_indices sent on this (non-parallel) path,
                        # so the worker default is identity — local index i
                        # (0-based: done_n - 1) IS the global index.
                        global_i = done_n - 1
                        wav_path = out_dir / f"chunk_{global_i:04d}.wav"
                        if wav_path.exists():
                            _safe_call(on_chunk_done, global_i, wav_path, what="on_chunk_done")
                elif on_log is not None:
                    heartbeat = HEARTBEAT_RE.match(line)
                    if heartbeat:
                        _safe_call(on_log, heartbeat.group(1), what="on_log")
                if stop_check is not None and stop_check():
                    proc.kill()
                    stopped = True
                    break
        proc.wait()

        results_path = out_dir / "results.json"
        if results_path.exists():
            results = json.loads(results_path.read_text())
        else:
            error_detail = ""
            if not stopped and stderr_path.exists():
                stderr_text = stderr_path.read_text(errors="replace").strip()
                if stderr_text:
                    error_detail = " — " + stderr_text.splitlines()[-1]
            results = {
                "chunks_processed": 0, "errors": (
                    [] if stopped else
                    [f"Worker process exited (code {proc.returncode}) without writing results.json{error_detail}"]
                ),
                "safety_abort": False,
            }
        results["engine"] = engine
        results["chunks_total"] = len(chunks)
        if stopped:
            results["stopped_by_user"] = True

        # Chunk files may have gaps (a mid-run chunk failure doesn't stop the
        # loop in the worker scripts) — scan the full chunk range by index,
        # not just the first `chunks_processed` files, to keep ordering correct.
        audio_arrays = []
        for i in range(len(chunks)):
            chunk_wav = out_dir / f"chunk_{i:04d}.wav"
            if chunk_wav.exists():
                audio, _sr = sf.read(str(chunk_wav), dtype="float32")
                audio_arrays.append(audio)
        return audio_arrays, results


def _synthesize_chapter_parallel(
    engine: str,
    chunks: list[str],
    reference_wav: str,
    device: str,
    on_progress: Callable[[int, int], None],
    stop_check: Callable[[], bool] | None,
    extra_config: dict | None,
    num_workers: int,
    on_log: Callable[[str], None] | None = None,
    on_chunk_done: Callable[[int, Path], None] | None = None,
) -> tuple[list[np.ndarray], dict]:
    """Fan `chunks` out across `num_workers` concurrent subprocesses, each
    handling a contiguous slice and loading its own model instance.

    Each worker gets its own tmp out_dir (no shared-directory write races)
    and writes chunk_{global_index:04d}.wav using the `chunk_indices` it was
    given, so results reassemble in original order regardless of which
    worker produced them.
    """
    py = venv_python(engine)
    script = ENGINE_CONFIG[engine]["script"]
    slices = _split_indices(len(chunks), num_workers)
    total = len(chunks)
    # See the single-worker path's comment above — "per_chunk" is sliced per
    # worker by `indices` here (unlike the rest of extra_config, which is
    # identical across every worker) since it's one entry per chunk.
    cfg = dict(extra_config or {})
    per_chunk = cfg.pop("per_chunk", None)

    with tempfile.TemporaryDirectory(prefix=f"scrolltone_{engine}_par_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        workers = []
        for worker_id, indices in enumerate(slices):
            worker_dir = tmp_path / f"worker_{worker_id}"
            worker_dir.mkdir()
            out_dir = worker_dir / "out"
            job_path = worker_dir / "job.json"
            job_path.write_text(json.dumps({
                "chunks":         [chunks[i] for i in indices],
                "chunk_indices":  indices,
                "reference_wav":  reference_wav,
                "out_dir":        str(out_dir),
                "device":         device,
                **cfg,
                **({"chunk_params": [per_chunk[i] for i in indices]} if per_chunk is not None else {}),
            }))
            stderr_path = worker_dir / "stderr.log"
            stderr_file = open(stderr_path, "w")
            proc = subprocess.Popen(
                [str(py), str(script), str(job_path)],
                stdout=subprocess.PIPE, stderr=stderr_file,
                text=True, bufsize=1,
            )
            workers.append({
                "id": worker_id, "indices": indices, "out_dir": out_dir,
                "stderr_path": stderr_path, "stderr_file": stderr_file,
                "proc": proc, "done": 0,
            })

        index_to_worker = {idx: w for w in workers for idx in w["indices"]}
        progress_lock = threading.Lock()
        stopped = {"flag": False}

        def _reader(worker):
            for line in worker["proc"].stdout:
                match = PROGRESS_RE.match(line)
                if match:
                    local_done = int(match.group(1))
                    with progress_lock:
                        worker["done"] = local_done
                        _safe_call(on_progress, sum(w["done"] for w in workers), total,
                                   what="on_progress")
                    if on_chunk_done is not None and 0 < local_done <= len(worker["indices"]):
                        global_i = worker["indices"][local_done - 1]
                        wav_path = worker["out_dir"] / f"chunk_{global_i:04d}.wav"
                        if wav_path.exists():
                            _safe_call(on_chunk_done, global_i, wav_path, what="on_chunk_done")
                elif on_log is not None:
                    heartbeat = HEARTBEAT_RE.match(line)
                    if heartbeat:
                        _safe_call(on_log, f"worker {worker['id']}: {heartbeat.group(1)}",
                                   what="on_log")

        def _watch_stop():
            if stop_check is None:
                return
            while any(w["proc"].poll() is None for w in workers):
                if stop_check():
                    stopped["flag"] = True
                    for w in workers:
                        w["proc"].kill()
                    return
                time.sleep(0.2)

        reader_threads = [threading.Thread(target=_reader, args=(w,), daemon=True) for w in workers]
        watch_thread = threading.Thread(target=_watch_stop, daemon=True)
        for t in reader_threads:
            t.start()
        watch_thread.start()

        for w in workers:
            w["proc"].wait()
        for t in reader_threads:
            t.join(timeout=2)
        watch_thread.join(timeout=1)
        for w in workers:
            w["stderr_file"].close()

        chunks_processed = 0
        errors = []
        safety_abort = False
        for w in workers:
            results_path = w["out_dir"] / "results.json"
            if results_path.exists():
                worker_results = json.loads(results_path.read_text())
                chunks_processed += worker_results.get("chunks_processed", 0)
                errors.extend(worker_results.get("errors", []))
                safety_abort = safety_abort or worker_results.get("safety_abort", False)
            elif not stopped["flag"]:
                stderr_text = w["stderr_path"].read_text(errors="replace").strip()
                detail = " — " + stderr_text.splitlines()[-1] if stderr_text else ""
                errors.append(
                    f"Worker {w['id']} exited (code {w['proc'].returncode}) "
                    f"without writing results.json{detail}"
                )

        results = {
            "engine":           engine,
            "chunks_total":     total,
            "chunks_processed": chunks_processed,
            "errors":           errors,
            "safety_abort":     safety_abort,
            "parallel_workers": len(workers),
        }
        if stopped["flag"]:
            results["stopped_by_user"] = True

        audio_arrays = []
        for i in range(total):
            worker = index_to_worker.get(i)
            if worker is None:
                continue
            chunk_wav = worker["out_dir"] / f"chunk_{i:04d}.wav"
            if chunk_wav.exists():
                audio, _sr = sf.read(str(chunk_wav), dtype="float32")
                audio_arrays.append(audio)
        return audio_arrays, results
