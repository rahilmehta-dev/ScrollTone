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


def synthesize_chapter(
    engine: str,
    chunks: list[str],
    reference_wav: str,
    device: str,
    on_progress: Callable[[int, int], None],
    stop_check: Callable[[], bool] | None = None,
    extra_config: dict | None = None,
    num_workers: int = 1,
) -> tuple[list[np.ndarray], dict]:
    """Run one chapter's chunks through an alternate engine's subprocess.

    `extra_config` is merged into the worker's job.json as extra top-level
    keys — e.g. {"temperature": 0.6} to override higgs_synth.py's default
    sampling temperature. Unrecognized keys are ignored by the worker script,
    so this is safe to pass even for a worker that doesn't use them.

    `num_workers` > 1 fans the chunk list out across that many concurrent
    subprocesses (each loading its own model instance — RAM scales roughly
    linearly with worker count) instead of one subprocess handling every
    chunk sequentially. Only worth it on CPU-only engines with cores/RAM to
    spare; see chatterbox_synth.py's module docstring for the RAM profile of
    a single worker.

    Returns (audio_arrays_in_order, results_dict). audio_arrays may be shorter
    than `chunks` if some failed or a safety abort truncated the run — check
    results_dict["errors"] / ["safety_abort"] for details.
    """
    if num_workers > 1 and len(chunks) > 1:
        return _synthesize_chapter_parallel(
            engine, chunks, reference_wav, device, on_progress, stop_check,
            extra_config, num_workers,
        )

    py = venv_python(engine)
    script = ENGINE_CONFIG[engine]["script"]

    with tempfile.TemporaryDirectory(prefix=f"scrolltone_{engine}_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        job_path = tmp_path / "job.json"
        out_dir = tmp_path / "out"
        job_path.write_text(json.dumps({
            "chunks": chunks,
            "reference_wav": reference_wav,
            "out_dir": str(out_dir),
            "device": device,
            **(extra_config or {}),
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
                    on_progress(int(match.group(1)), int(match.group(2)))
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
                **(extra_config or {}),
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
                    with progress_lock:
                        worker["done"] = int(match.group(1))
                        on_progress(sum(w["done"] for w in workers), total)

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
