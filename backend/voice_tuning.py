"""Auto-tune: search Chatterbox/Higgs synthesis parameters for one or more
reference voice clips, score each candidate with an open-source no-reference
audio-quality model, and surface the best-scoring config per clip — so a
user doesn't have to manually A/B Advanced Settings sliders by ear.

Design notes:
- Candidates are chosen via Latin Hypercube Sampling (scipy.stats.qmc) over
  each engine's tunable parameter ranges. LHS gives even, stratified
  coverage of the parameter space for a small sample budget — better than a
  full grid (which blows up combinatorially: 3 params x 3 levels = 27
  candidates, way over budget at ~4 min/candidate) or plain random (which
  can cluster several samples in the same region by chance). Bayesian
  optimization would be more sample-efficient per candidate, but it's
  inherently sequential (pick a point, score it, pick the next) — that
  kills the parallelism this needs to finish in reasonable wall-clock time,
  since LHS's whole candidate set is known upfront and runs across
  chatterbox_workers/higgs_workers just like a real chapter's chunks would.
- Every candidate synthesizes the SAME sample text (PREVIEW_JOB_TEXT)
  against the SAME reference clip — only the sampling params vary —
  dispatched as one "chapter" of N identical chunks with a chunk_params
  override each (runner.py's `per_chunk` mechanism). This reuses the exact
  subprocess/parallel-worker machinery a real conversion already uses,
  instead of a bespoke second code path.
- Scoring uses DNSMOS (Microsoft's `speechmos` package: pip install
  speechmos librosa onnxruntime) — non-intrusive, fully offline, bundled
  ONNX models, no checkpoint download, no torch dependency growth. It's
  tuned for noise/artifact evaluation more than pure TTS naturalness
  (unlike UTMOS, which is trained specifically on TTS naturalness ratings
  but has no cleanly-packaged, reliable pip install), but on clean
  synthesized speech with no background noise its `ovrl_mos` still tracks
  how clean/natural vs. distorted-and-robotic a clip sounds — a reasonable,
  honest choice given what's actually easy to run offline today.
- Each candidate is scored the moment its wav is written (via
  synthesize_chapter's on_chunk_done callback — see runner.py), not after
  the whole batch finishes. The worker scripts write a chunk's file before
  printing its progress line, so by the time that progress signal reaches
  the parent process the file is guaranteed to already exist on disk —
  this reuses that ordering instead of adding a second wait/poll step. That
  means a long batch shows a live, shuffling "best so far" instead of going
  silent until every candidate is done.

Two entry points share the per-voice search core (_autotune_one_voice):
- run_autotune_job       — one reference clip (the Advanced Settings button)
- run_batch_autotune_job — several reference clips in one job (the
                            standalone Voice Lab tool page), reporting each
                            voice's ranked results as it finishes so a long
                            batch is watchable incrementally, not a black box.
"""
import shutil
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

from backend.engines.runner import synthesize_chapter, EngineNotInstalled
from backend.voices import PREVIEW_JOB_TEXT


def _fmt_duration(seconds: float) -> str:
    """1834.2 -> '30m 34s'. Used to stamp how long each voice/the whole job
    actually took directly into the log text (not just relying on the
    frontend's per-line arrival timestamp) — useful for a batch left running
    unattended, checked the next morning instead of watched live."""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"

# Chatterbox's `exaggeration` is deliberately excluded from the search — it's
# baked into prepare_conditionals(), computed once and reused across chunks
# to avoid a known memory-growth issue (see chatterbox_synth.py's
# module docstring), so it can't safely vary per-candidate within one job.
# Only the two params that are safe to vary per generate() call are searched.
#
# Both chatterbox ranges below are narrowed from the sliders' full technical
# span (0-1 / 0.05-1.5) to the region actually associated with natural-
# sounding output, per this app's own hand-tuned defaults and the tradeoffs
# already documented in frontend/index.html's tooltips:
#   cfg_weight  — too low drifts from the reference / sounds unstable; too
#                 high sounds "flat, monotone, robotic". Default is 0.3.
#   temperature — too low reads as "reading a script" sameness; too high
#                 risks "slurring/artifacts". Default is 0.8.
# Searching the full range would waste candidates on combos already known to
# sound bad at either extreme, when every candidate costs real minutes.
PARAM_RANGES = {
    "chatterbox": {
        "cfg_weight":  (0.15, 0.5),
        "temperature": (0.55, 1.05),
    },
    "higgs": {
        "temperature": (0.05, 1.5),
        "top_p":       (0.1, 1.0),
        "top_k":       (1, 200),
    },
}

MIN_CANDIDATES = 3
MAX_CANDIDATES = 30
DEFAULT_CANDIDATES = 20

# Fixed by default (not left to scipy's own random seeding) so a run is
# reproducible — same LHS combos on a rerun, and every voice in one batch
# gets the exact same candidate parameter sets, making cross-voice score
# comparisons apples-to-apples instead of each voice being scored against a
# different, independently-random slice of the parameter space.
DEFAULT_SEED = 411

MIN_VOICES = 1
MAX_VOICES = 8


def sample_lhs(engine: str, n: int, seed: int | None = None) -> list[dict]:
    """N parameter combos for `engine`, space-filling via Latin Hypercube
    Sampling over PARAM_RANGES[engine]. Each combo is a dict of real
    (already range-scaled) parameter values, ready to use as a
    chunk_params entry passed through to the engine's worker script.

    Import is local, like _score_one_candidate's speechmos import below —
    scipy isn't in the base Docker image (kept slim on purpose; see
    documentation/engines.md's Auto-tune setup note), so importing it at
    module level would crash the whole app on startup wherever it's
    missing, not just this one feature.
    """
    from scipy.stats import qmc
    ranges = PARAM_RANGES[engine]
    names = list(ranges.keys())
    sampler = qmc.LatinHypercube(d=len(names), seed=seed)
    unit_samples = sampler.random(n=n)
    lo = np.array([ranges[name][0] for name in names])
    hi = np.array([ranges[name][1] for name in names])
    scaled = qmc.scale(unit_samples, lo, hi)

    combos = []
    for row in scaled:
        combo = dict(zip(names, row.tolist()))
        if "top_k" in combo:
            combo["top_k"] = round(combo["top_k"])
        combos.append(combo)
    return combos


def _score_one_candidate(path: Path) -> float:
    """DNSMOS overall-quality score (roughly 1-5, higher = better) for one
    wav. Import is local — speechmos/librosa/onnxruntime (and librosa's
    numba JIT warmup) are only needed for this one feature, no reason to
    pay that import cost on every app start.
    """
    from speechmos import dnsmos
    return float(dnsmos.run(str(path), sr=16000)["ovrl_mos"])


def _autotune_one_voice(
    *, engine: str, reference_wav: str, device: str, num_workers: int, n: int,
    out_dir: str, filename_prefix: str, seed: int | None, ch_i: int,
    log: Callable[[str], None], status: Callable[[str], None],
    push: Callable[[dict], None], stop_event: threading.Event,
) -> list[dict] | None:
    """Runs one voice's auto-tune search end-to-end: sample LHS combos,
    synthesize each against `reference_wav` (in parallel across
    `num_workers`), scoring each candidate with DNSMOS AS SOON AS ITS WAV
    IS WRITTEN (via synthesize_chapter's on_chunk_done — no waiting for
    every candidate to finish before any of them get scored). Writes
    `{filename_prefix}candidate_NN.wav` files into `out_dir` — the prefix
    keeps filenames unique when multiple voices share one out_dir (batch
    mode); pass "" for a single-voice job.

    Pushes one {"type": "autotune_candidate_result", ...} message per
    candidate the moment it's scored (includes a running "is this a new
    best for this voice" flag), in addition to returning the final ranked
    list once every candidate that finished has been scored.

    Returns the ranked results list (each {"params", "score", "filename"}),
    or None if the job was stopped mid-run or nothing was synthesized —
    either way, the reason is already logged, so the caller just needs to
    decide what None means for its surrounding job (abort entirely vs. skip
    this one voice and continue to the next).
    """
    combos = sample_lhs(engine, n, seed=seed)
    for i, combo in enumerate(combos):
        log(f"   candidate {i + 1}: " + ", ".join(f"{k}={v:.3g}" for k, v in combo.items()))

    push({"type": "ch_start", "ch_i": ch_i, "chunks": n})
    chunks = [PREVIEW_JOB_TEXT] * n

    def _on_progress(done_n, total_n):
        push({"type": "ch_prog", "ch_i": ch_i, "pct": round(done_n / total_n, 3)})

    # Mutated from possibly-concurrent reader threads (one per worker, see
    # runner.py's _synthesize_chapter_parallel) — the lock serializes both
    # the DNSMOS call itself (its thread-safety under concurrent Run() calls
    # isn't something this app controls) and the best-score compare-and-set,
    # which would otherwise race between two candidates finishing at once.
    live_results: dict[int, dict] = {}
    best_score = {"value": None}
    score_lock = threading.Lock()

    def _on_chunk_done(global_i: int, tmp_wav_path: Path):
        # Persist immediately — tmp_wav_path lives inside synthesize_chapter's
        # TemporaryDirectory, deleted as soon as that call returns.
        filename = f"{filename_prefix}candidate_{global_i:02d}.wav"
        final_path = Path(out_dir) / filename
        shutil.copyfile(tmp_wav_path, final_path)

        with score_lock:
            score = round(_score_one_candidate(final_path), 3)
            is_new_best = best_score["value"] is None or score > best_score["value"]
            if is_new_best:
                best_score["value"] = score
            # Snapshot inside the lock: read outside it, a candidate
            # finishing on another worker's reader thread could overwrite
            # best_score between the compare above and the push below,
            # reporting is_new_best=True alongside someone else's higher
            # best_score — a leaderboard that contradicts itself.
            best_so_far = best_score["value"]
            result = {"params": combos[global_i], "score": score, "filename": filename}
            live_results[global_i] = result

        log(f"   candidate {global_i + 1}/{n} scored {score:.2f}"
            + ("  ← new best" if is_new_best else ""))
        push({
            "type": "autotune_candidate_result", "ch_i": ch_i,
            "candidate_index": global_i, "params": combos[global_i],
            "score": score, "filename": filename,
            "is_new_best": is_new_best, "best_score": best_so_far,
        })

    try:
        _audio_arrays, engine_result = synthesize_chapter(
            engine, chunks, reference_wav, device,
            on_progress=_on_progress,
            stop_check=stop_event.is_set,
            extra_config={"per_chunk": combos},
            num_workers=num_workers,
            on_log=lambda msg: log(f"   [{engine}] {msg}"),
            on_chunk_done=_on_chunk_done,
        )
    except EngineNotInstalled as error:
        log(f"   ! {error}")
        return None

    if stop_event.is_set():
        return None

    for err in engine_result.get("errors", []):
        log(f"   ! [{engine}] {err}")

    if not live_results:
        log("   No candidates synthesized for this voice — nothing to score.")
        return None

    results = sorted(live_results.values(), key=lambda r: r["score"], reverse=True)

    log("   Results (ranked by predicted naturalness):")
    for rank, r in enumerate(results, 1):
        param_str = ", ".join(f"{k}={v:.3g}" for k, v in r["params"].items())
        log(f"      #{rank}  score={r['score']:.2f}  {param_str}  → {r['filename']}")

    return results


def run_autotune_job(job_state: dict, settings: dict, loop) -> None:
    """Auto-tune job: sample N parameter combos, synthesize the same short
    sample text with each against the uploaded reference clip (in parallel,
    reusing the engine's normal worker infrastructure), score every
    candidate, and report the ranked results.

    Same job/SSE machinery as Preview & Tweak (backend/pipeline.py's
    run_preview_job) — routes/convert.py's /stream/{id}, /stop/{id}, and
    /download/{id}/{file} all work unchanged on the job_id this produces.
    Reports two extra message types the frontend doesn't otherwise see: one
    "autotune_candidate_result" per candidate as it's scored (live, via
    _autotune_one_voice — includes a running is_new_best flag), then a
    final {"type": "autotune_result", "candidates": [...], "winner": {...}}
    once every candidate is in.
    """
    from backend.job_events import JobEmitter
    emitter = JobEmitter(job_state, loop)
    log, status, push, done = emitter.log, emitter.status, emitter.push, emitter.done

    try:
        job_start = time.time()
        engine = settings["engine"]
        if engine not in PARAM_RANGES:
            log(f"Auto-tune isn't supported for engine={engine!r} "
                f"(only chatterbox/higgs have tunable synthesis params to search).")
            job_state["status"] = "error"
            done()
            return

        n = settings.get("num_candidates", DEFAULT_CANDIDATES)
        seed = settings.get("seed", DEFAULT_SEED)
        log(f"Auto-tune: {engine}  |  {n} candidates (seed={seed})  |  "
            f"reference={Path(settings['reference_wav']).name}  |  "
            f"started {time.strftime('%H:%M:%S')}\n")
        status(f"Sampling {n} candidate parameter sets…")
        push({"type": "ch_info", "chapters": [{"i": 0, "title": "Auto-tune"}]})

        num_workers = min(n, settings.get(f"{engine}_workers", 1))
        status(f"Synthesizing {n} candidates "
               f"({num_workers} parallel worker{'s' if num_workers > 1 else ''})…")

        voice_start = time.time()
        results = _autotune_one_voice(
            engine=engine, reference_wav=settings["reference_wav"], device=settings["device"],
            num_workers=num_workers, n=n, out_dir=settings["out_dir"], filename_prefix="",
            seed=seed, ch_i=0, log=log, status=status, push=push,
            stop_event=job_state["stop_event"],
        )
        voice_elapsed = time.time() - voice_start

        if job_state["stop_event"].is_set():
            log(f"\nStopped by user after {_fmt_duration(voice_elapsed)}.")
            job_state["status"] = "cancelled"
            done()
            return

        if results is None:
            job_state["status"] = "error"
            done()
            return

        winner = results[0]
        log(f"\nBest: {winner['filename']} (score {winner['score']:.2f})  "
            f"— synthesis + scoring took {_fmt_duration(voice_elapsed)}")
        job_state["files"].extend(r["filename"] for r in results)

        push({"type": "autotune_result", "candidates": results, "winner": winner})

        job_state["status"] = "done"
        log(f"\nDone — total {_fmt_duration(time.time() - job_start)}, "
            f"finished {time.strftime('%H:%M:%S')}.")
        done()

    except Exception as error:
        import traceback
        log(f"\nError: {error}")
        log(traceback.format_exc())
        job_state["status"] = "error"
        done()


def run_batch_autotune_job(job_state: dict, settings: dict, loop) -> None:
    """Batch auto-tune (the Voice Lab tool page): runs the same search
    independently for each of several uploaded reference clips, one after
    another, reporting each voice's ranked results as soon as it finishes —
    so comparing several candidate voice actors/clips is one job instead of
    running Auto-tune once per clip by hand.

    `settings["voices"]` is [{"filename": display_name, "path": str}, ...].
    Same job/SSE machinery as run_autotune_job, including its per-candidate
    "autotune_candidate_result" live-scoring messages (tagged with "ch_i" —
    the voice index — so a listener can tell which voice's leaderboard to
    update), plus one extra message type per completed voice (not per whole
    job): {"type": "autotune_voice_result", "voice_index": i,
    "voice_filename": ..., "candidates": [...], "winner": {...}}
    A voice that fails or is stopped mid-run is skipped (ch_skip pushed,
    logged) rather than aborting the whole batch — later voices still run.
    """
    from backend.job_events import JobEmitter
    emitter = JobEmitter(job_state, loop)
    log, status, push, done = emitter.log, emitter.status, emitter.push, emitter.done

    try:
        batch_start = time.time()
        engine = settings["engine"]
        if engine not in PARAM_RANGES:
            log(f"Auto-tune isn't supported for engine={engine!r} "
                f"(only chatterbox/higgs have tunable synthesis params to search).")
            job_state["status"] = "error"
            done()
            return

        voices = settings["voices"]
        n = settings.get("num_candidates", DEFAULT_CANDIDATES)
        seed = settings.get("seed", DEFAULT_SEED)
        num_workers = min(n, settings.get(f"{engine}_workers", 1))

        log(f"Batch auto-tune: {engine}  |  {len(voices)} voice(s)  |  "
            f"{n} candidates each (seed={seed})  ({num_workers} parallel worker"
            f"{'s' if num_workers > 1 else ''})  |  started {time.strftime('%H:%M:%S')}\n")
        push({"type": "ch_info", "chapters": [
            {"i": i, "title": v["filename"]} for i, v in enumerate(voices)
        ]})

        summary = []
        for i, voice in enumerate(voices):
            if job_state["stop_event"].is_set():
                log(f"\nStopped by user after {_fmt_duration(time.time() - batch_start)}.")
                job_state["status"] = "cancelled"
                done()
                return

            status(f"Voice {i + 1}/{len(voices)}: {voice['filename']}…")
            log(f"── Voice {i + 1}/{len(voices)}: {voice['filename']}  "
                f"[{time.strftime('%H:%M:%S')}]")

            voice_start = time.time()
            results = _autotune_one_voice(
                engine=engine, reference_wav=voice["path"], device=settings["device"],
                num_workers=num_workers, n=n, out_dir=settings["out_dir"],
                filename_prefix=f"v{i:02d}_", seed=seed, ch_i=i,
                log=log, status=status, push=push, stop_event=job_state["stop_event"],
            )
            voice_elapsed = time.time() - voice_start

            if job_state["stop_event"].is_set():
                log(f"\nStopped by user after {_fmt_duration(time.time() - batch_start)}.")
                job_state["status"] = "cancelled"
                done()
                return

            if results is None:
                push({"type": "ch_skip", "ch_i": i})
                log(f"   Skipped {voice['filename']} after {_fmt_duration(voice_elapsed)} "
                    f"— see errors above.\n")
                continue

            winner = results[0]
            job_state["files"].extend(r["filename"] for r in results)
            push({
                "type": "autotune_voice_result", "voice_index": i,
                "voice_filename": voice["filename"], "candidates": results, "winner": winner,
            })
            summary.append({"voice_filename": voice["filename"], "winner": winner,
                             "elapsed": voice_elapsed})
            log(f"   Best for {voice['filename']}: {winner['filename']} "
                f"(score {winner['score']:.2f})  — took {_fmt_duration(voice_elapsed)}\n")

        total_elapsed = time.time() - batch_start
        log("\n" + "=" * 40)
        log(f"Batch summary — {len(summary)}/{len(voices)} voice(s) completed, "
            f"total {_fmt_duration(total_elapsed)}:")
        for s in summary:
            param_str = ", ".join(f"{k}={v:.3g}" for k, v in s["winner"]["params"].items())
            log(f"   {s['voice_filename']}: score={s['winner']['score']:.2f}  "
                f"{param_str}  ({_fmt_duration(s['elapsed'])})")
        if not summary:
            log("   No voice produced usable results.")

        job_state["status"] = "done"
        log(f"\nDone — total {_fmt_duration(total_elapsed)}, finished {time.strftime('%H:%M:%S')}.")
        done()

    except Exception as error:
        import traceback
        log(f"\nError: {error}")
        log(traceback.format_exc())
        job_state["status"] = "error"
        done()
