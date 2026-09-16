"""Standalone Chatterbox TTS synthesis worker.

Not part of the main app's import graph — invoked as a subprocess via the
`.venv-chatterbox` interpreter (backend/pipeline.py launches it per chapter).
See documentation/engines.md.

CRITICAL: device is hardcoded to "cpu" and NOT read from the job file or any
setting. Chatterbox's autoregressive decoder has a reproducible, severe memory
leak on Apple's MPS backend — confirmed twice this project to grow past 78GB
RSS (forcing ~36GB into swap) *inside a single generate() call*, well before
any inter-chunk safety check could fire. CPU was confirmed stable (~6.6GB peak
for a full chapter). Do not make this configurable without re-validating MPS
safety from scratch. (This was briefly made configurable at explicit user
request — see git history around 2026-08-28 — then reverted back to
hardcoded CPU shortly after, also at user request.)

Usage:
    .venv-chatterbox/bin/python backend/engines/chatterbox_synth.py <job.json>

job.json:
    {"chunks": [str, ...], "reference_wav": str, "out_dir": str,
     "chunk_indices": [int, ...] (optional), "chunk_params": [dict, ...] (optional)}

`chunk_params`, if given, is one {"cfg_weight"/"temperature": ...} dict per
chunk, overriding the job-level defaults for that chunk only (NOT
"exaggeration" — see the comment above prepare_conditionals() below for
why). Used by backend/voice_tuning.py's auto-tune job to test several
sampling configs against the same sample text in one run.

Writes chunk_0000.wav, chunk_0001.wav, ... (one per successfully synthesized
chunk, gaps on failure) into out_dir, plus results.json summarizing the run.
Progress is reported on stdout as lines matching PROGRESS_PREFIX, which the
parent process greps for — everything else on stdout/stderr is noise, EXCEPT
lines matching _heartbeat.HEARTBEAT_PREFIX, which the parent forwards to the
job log so a long silent stretch (model load, or a single generate() call)
doesn't look indistinguishable from a hang — see _heartbeat.py.
"""
import gc
import json
import resource
import sys
from pathlib import Path

import _heartbeat

PROGRESS_PREFIX = "##PROGRESS##"
SAFE_RSS_LIMIT_GB = 20.0  # defense-in-depth; CPU peaked ~6.6GB in testing
# Defaults, overridable per-job via job.json (see ENGINE_CONFIG's extra_config
# passthrough in runner.py) — tuned lower cfg_weight / raised exaggeration
# vs. the library's own defaults (0.5 / 0.5) for less flat, less robotic
# delivery. See documentation/settings.md for what each knob does.
CFG_WEIGHT = 0.3
EXAGGERATION = 0.7
TEMPERATURE = 0.8


def rss_gb() -> float:
    # ru_maxrss is in BYTES on macOS but KILOBYTES on Linux (getrusage(2)).
    # Without this scaling the Linux/Docker path — the primary deployment
    # target — under-reports memory by 1024x, so the safety abort below can
    # never fire and the memory guard silently does nothing.
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != "darwin":
        rss *= 1024
    return rss / (1024.0 ** 3)


def main():
    if len(sys.argv) != 2:
        print("usage: chatterbox_synth.py <job.json>", file=sys.stderr)
        return 1

    job = json.loads(Path(sys.argv[1]).read_text())
    chunks = job["chunks"]
    reference_wav = job["reference_wav"]
    out_dir = Path(job["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    # Set by runner.py's parallel dispatch — maps each local chunk to its
    # position in the full chapter, so output filenames stay globally
    # ordered even though this worker only sees a slice of the chunk list.
    chunk_indices = job.get("chunk_indices") or list(range(len(chunks)))

    cfg_weight   = float(job.get("cfg_weight", CFG_WEIGHT))
    exaggeration = float(job.get("exaggeration", EXAGGERATION))
    temperature  = float(job.get("temperature", TEMPERATURE))
    # Set by runner.py when the caller passed extra_config["per_chunk"] (see
    # its docstring) — e.g. voice_tuning.py's auto-tune job, testing several
    # {cfg_weight, temperature} combos against the same sample text. NOT
    # exaggeration: it's baked into prepare_conditionals() below, computed
    # once and reused across chunks on purpose (recomputing it per-chunk was
    # the likely cause of the memory-growth issue mentioned in that comment)
    # — so exaggeration stays fixed at the job-level value for every chunk
    # even when chunk_params is given, and auto-tune only searches the two
    # params that don't require recomputing conditionals.
    chunk_params = job.get("chunk_params")

    result = {
        "engine": "chatterbox",
        "device_used": "cpu",  # always — see module docstring
        "chunks_total": len(chunks),
        "chunks_processed": 0,
        "errors": [],
        "safety_abort": False,
        "config": {"cfg_weight": cfg_weight, "exaggeration": exaggeration, "temperature": temperature},
    }

    stage, heartbeat_stop = _heartbeat.start("loading model (device=cpu)")

    try:
        import torchaudio
        from chatterbox.tts import ChatterboxTTS

        try:
            model = ChatterboxTTS.from_pretrained(device="cpu")
        except Exception as e:  # noqa: BLE001
            result["errors"].append(f"Model failed to load: {type(e).__name__}: {e}")
            return 1

        # Computed once, reused across chunks — recomputing per-chunk (passing
        # audio_prompt_path to every generate() call) was the likely cause of an
        # earlier, separate memory-growth issue before this was fixed.
        stage["text"] = "preparing voice conditionals"
        model.prepare_conditionals(reference_wav, exaggeration=exaggeration)

        sr = model.sr
        for i, chunk_text in enumerate(chunks):
            global_i = chunk_indices[i]
            stage["text"] = f"generating chunk {i + 1}/{len(chunks)} (global #{global_i})"
            chunk_cfg_weight  = cfg_weight
            chunk_temperature = temperature
            if chunk_params is not None:
                overrides = chunk_params[i] or {}
                chunk_cfg_weight  = float(overrides.get("cfg_weight", cfg_weight))
                chunk_temperature = float(overrides.get("temperature", temperature))
            try:
                wav = model.generate(
                    chunk_text, cfg_weight=chunk_cfg_weight, exaggeration=exaggeration,
                    temperature=chunk_temperature,
                )
                torchaudio.save(str(out_dir / f"chunk_{global_i:04d}.wav"), wav.detach().cpu(), sr)
                result["chunks_processed"] += 1
                _heartbeat.emit(f"{PROGRESS_PREFIX} {i + 1} {len(chunks)}")

                del wav
                gc.collect()

                current_rss = rss_gb()
                if current_rss > SAFE_RSS_LIMIT_GB:
                    result["errors"].append(
                        f"Safety abort: RSS hit {current_rss:.1f}GB (limit {SAFE_RSS_LIMIT_GB}GB) "
                        f"after chunk {i + 1}/{len(chunks)} (global #{global_i})"
                    )
                    result["safety_abort"] = True
                    break
            except Exception as e:  # noqa: BLE001
                result["errors"].append(f"chunk {global_i}: {type(e).__name__}: {e}")
                print(f"[chatterbox] chunk {global_i} FAILED: {e}", file=sys.stderr)

        return 0
    finally:
        heartbeat_stop.set()
        (out_dir / "results.json").write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    sys.exit(main())
