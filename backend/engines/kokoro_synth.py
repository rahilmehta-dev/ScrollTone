"""Standalone Kokoro synthesis worker — used only for Kokoro Parallel Workers.

Not part of the main app's import graph — invoked as a subprocess via the
*same* interpreter as the main app (backend/pipeline.py launches it per
chapter, only when kokoro_workers > 1). Unlike Higgs/Chatterbox, Kokoro's
dependencies already live in the main venv — there's no separate
.venv-kokoro — so this script needs nothing installed beyond what running
ScrollTone at all already requires.

The single-worker (default) path never touches this file: chapter_processor.py
keeps calling Kokoro in-process, which is faster for the common case (no
subprocess/model-load overhead). This script exists purely so multiple
independent Kokoro model instances — one per worker process — can run truly
in parallel across CPU cores without fighting over a shared instance or the
GIL, mirroring how Chatterbox's parallel workers already work.

Usage:
    <python> backend/engines/kokoro_synth.py <job.json>

job.json:
    {"chunks": [str, ...], "voice": str, "speed": float, "lang_code": str,
     "device": str, "out_dir": str, "chunk_indices": [int, ...] (optional)}

Writes chunk_0000.wav, chunk_0001.wav, ... (one per successfully synthesized
chunk, gaps on failure) into out_dir, plus results.json summarizing the run.
Progress is reported on stdout as lines matching PROGRESS_PREFIX, which the
parent process greps for — everything else on stdout/stderr is noise.
"""
import json
import sys
from pathlib import Path

PROGRESS_PREFIX = "##PROGRESS##"


def main():
    if len(sys.argv) != 2:
        print("usage: kokoro_synth.py <job.json>", file=sys.stderr)
        return 1

    job = json.loads(Path(sys.argv[1]).read_text())
    chunks    = job["chunks"]
    voice     = job["voice"]
    speed     = float(job.get("speed", 1.0))
    lang_code = job.get("lang_code", "a")
    device    = job.get("device", "cpu")
    out_dir   = Path(job["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    # Set by runner.py's parallel dispatch — maps each local chunk to its
    # position in the full chapter, so output filenames stay globally
    # ordered even though this worker only sees a slice of the chunk list.
    chunk_indices = job.get("chunk_indices") or list(range(len(chunks)))

    result = {
        "engine": "kokoro",
        "chunks_total": len(chunks),
        "chunks_processed": 0,
        "errors": [],
        "safety_abort": False,
    }

    # This process exists specifically to run alongside N-1 siblings, each
    # wanting its own share of the cores — without capping intra-op threads,
    # every worker's own matrix ops would each try to claim every core,
    # oversubscribing the machine and making N workers slower than one.
    import torch
    torch.set_num_threads(1)

    import numpy as np
    import soundfile as sf
    from kokoro import KPipeline

    try:
        pipeline = KPipeline(lang_code=lang_code, repo_id="hexgrad/Kokoro-82M", device=device)
    except Exception as e:  # noqa: BLE001
        result["errors"].append(f"Model failed to load: {type(e).__name__}: {e}")
        (out_dir / "results.json").write_text(json.dumps(result, indent=2))
        return 1

    for i, chunk_text in enumerate(chunks):
        global_i = chunk_indices[i]
        try:
            audio_parts = [audio for _, _, audio in pipeline(chunk_text, voice=voice, speed=speed)]
            if audio_parts:
                combined = np.concatenate(audio_parts)
                sf.write(str(out_dir / f"chunk_{global_i:04d}.wav"), combined, 24000)
            result["chunks_processed"] += 1
            print(f"{PROGRESS_PREFIX} {i + 1} {len(chunks)}", flush=True)
        except Exception as e:  # noqa: BLE001
            result["errors"].append(f"chunk {global_i}: {type(e).__name__}: {e}")
            print(f"[kokoro] chunk {global_i} FAILED: {e}", file=sys.stderr)

    (out_dir / "results.json").write_text(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
