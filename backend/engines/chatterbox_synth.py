"""Standalone Chatterbox TTS synthesis worker.

Not part of the main app's import graph — invoked as a subprocess via the
`.venv-chatterbox` interpreter (backend/pipeline.py launches it per chapter).
See README "Optional: Higgs Audio V2 / Chatterbox engines".

CRITICAL: device is hardcoded to "cpu" and NOT read from the job file or any
setting. Chatterbox's autoregressive decoder has a reproducible, severe memory
leak on Apple's MPS backend — confirmed twice this project to grow past 78GB
RSS (forcing ~36GB into swap) *inside a single generate() call*, well before
any inter-chunk safety check could fire. CPU was confirmed stable (~6.6GB peak
for a full chapter). Do not make this configurable without re-validating MPS
safety from scratch.

Usage:
    .venv-chatterbox/bin/python backend/engines/chatterbox_synth.py <job.json>

job.json:
    {"chunks": [str, ...], "reference_wav": str, "out_dir": str}

Writes chunk_0000.wav, chunk_0001.wav, ... (one per successfully synthesized
chunk, gaps on failure) into out_dir, plus results.json summarizing the run.
Progress is reported on stdout as lines matching PROGRESS_PREFIX, which the
parent process greps for — everything else on stdout/stderr is noise.
"""
import gc
import json
import resource
import sys
from pathlib import Path

PROGRESS_PREFIX = "##PROGRESS##"
SAFE_RSS_LIMIT_GB = 20.0  # defense-in-depth; CPU peaked ~6.6GB in testing
# Defaults, overridable per-job via job.json (see ENGINE_CONFIG's extra_config
# passthrough in runner.py) — tuned lower cfg_weight / raised exaggeration
# vs. the library's own defaults (0.5 / 0.5) for less flat, less robotic
# delivery. See README for what each knob does.
CFG_WEIGHT = 0.3
EXAGGERATION = 0.7
TEMPERATURE = 0.8


def rss_gb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024.0 ** 3)


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

    result = {
        "engine": "chatterbox",
        "device_used": "cpu",  # always — see module docstring
        "chunks_total": len(chunks),
        "chunks_processed": 0,
        "errors": [],
        "safety_abort": False,
        "config": {"cfg_weight": cfg_weight, "exaggeration": exaggeration, "temperature": temperature},
    }

    import torchaudio
    from chatterbox.tts import ChatterboxTTS

    try:
        model = ChatterboxTTS.from_pretrained(device="cpu")
    except Exception as e:  # noqa: BLE001
        result["errors"].append(f"Model failed to load: {type(e).__name__}: {e}")
        (out_dir / "results.json").write_text(json.dumps(result, indent=2))
        return 1

    # Computed once, reused across chunks — recomputing per-chunk (passing
    # audio_prompt_path to every generate() call) was the likely cause of an
    # earlier, separate memory-growth issue before this was fixed.
    model.prepare_conditionals(reference_wav, exaggeration=exaggeration)

    sr = model.sr
    for i, chunk_text in enumerate(chunks):
        global_i = chunk_indices[i]
        try:
            wav = model.generate(
                chunk_text, cfg_weight=cfg_weight, exaggeration=exaggeration, temperature=temperature,
            )
            torchaudio.save(str(out_dir / f"chunk_{global_i:04d}.wav"), wav.detach().cpu(), sr)
            result["chunks_processed"] += 1
            print(f"{PROGRESS_PREFIX} {i + 1} {len(chunks)}", flush=True)

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

    (out_dir / "results.json").write_text(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
