"""Standalone Higgs Audio V2 synthesis worker.

Not part of the main app's import graph — invoked as a subprocess via the
`.venv-higgs` interpreter (backend/pipeline.py launches it per chapter), so it
can depend on transformers/torch without those being installed in the main
ScrollTone venv. See documentation/engines.md.

Usage:
    .venv-higgs/bin/python backend/engines/higgs_synth.py <job.json>

job.json:
    {"chunks": [str, ...], "reference_wav": str, "out_dir": str, "device": "mps"|"cpu"|"cuda",
     "chunk_indices": [int, ...] (optional), "chunk_params": [dict, ...] (optional)}

`chunk_indices` — set by runner.py's parallel dispatch — maps each local
chunk to its position in the full chapter, so output filenames stay
globally ordered even though this worker only sees a slice of the chunk
list (same as chatterbox_synth.py / kokoro_synth.py).

`chunk_params`, if given, is one {"temperature"/"top_p"/"top_k": ...} dict
per chunk, overriding the job-level defaults for that chunk only — used by
backend/voice_tuning.py's auto-tune job to test several sampling configs
against the same sample text in one run (see runner.py's `per_chunk` note).

Writes chunk_0000.wav, chunk_0001.wav, ... (one per successfully synthesized
chunk, gaps on failure) into out_dir, plus results.json summarizing the run.
Progress is reported on stdout as lines matching PROGRESS_PREFIX, which the
parent process greps for — everything else on stdout/stderr (model-loading
logs, tqdm bars, transformers warnings) is noise the parent ignores, EXCEPT
lines matching _heartbeat.HEARTBEAT_PREFIX, which the parent forwards to the
job log so a long silent stretch (loading the ~11.8GB model, or a single
generate() call that can run a minute+) doesn't look indistinguishable from
a hang — see _heartbeat.py.
"""
import gc
import json
import resource
import sys
from pathlib import Path

import _heartbeat

MODEL_ID = "bosonai/higgs-audio-v2-generation-3B-base"
PROGRESS_PREFIX = "##PROGRESS##"
SAFE_RSS_LIMIT_GB = 24.0  # defense-in-depth; Higgs peaked ~12GB in testing

SCENE_PROMPT = "Audio is recorded from a quiet room."


def rss_gb() -> float:
    # ru_maxrss is in BYTES on macOS but KILOBYTES on Linux (getrusage(2)).
    # Without this scaling the Linux/Docker path — the primary deployment
    # target — under-reports memory by 1024x, so the safety abort below can
    # never fire and the memory guard silently does nothing.
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != "darwin":
        rss *= 1024
    return rss / (1024.0 ** 3)


def build_conversation(reference_wav: str, text: str):
    return [
        {"role": "system", "content": [{"type": "text", "text": "Generate audio following instruction."}]},
        {"role": "scene", "content": [{"type": "text", "text": SCENE_PROMPT}]},
        {"role": "user", "content": [{"type": "text", "text": "This is a short reference clip of the narrator's voice."}]},
        {"role": "assistant", "content": [{"type": "audio", "path": reference_wav}]},
        {"role": "user", "content": [{"type": "text", "text": text}]},
    ]


def main():
    if len(sys.argv) != 2:
        print("usage: higgs_synth.py <job.json>", file=sys.stderr)
        return 1

    job = json.loads(Path(sys.argv[1]).read_text())
    chunks = job["chunks"]
    reference_wav = job["reference_wav"]
    out_dir = Path(job["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    requested_device = job.get("device", "mps")
    # Set by runner.py's parallel dispatch — maps each local chunk to its
    # position in the full chapter, so output filenames stay globally
    # ordered even though this worker only sees a slice of the chunk list
    # (same as chatterbox_synth.py / kokoro_synth.py).
    chunk_indices = job.get("chunk_indices") or list(range(len(chunks)))
    chunk_params  = job.get("chunk_params")

    # Every chunk here is an independent sampling call with no memory of the
    # last (see module docstring's lack of any cross-chunk state) — Boson's
    # own defaults (temperature=1.0, top_p=0.95, top_k=50) are tuned for
    # expressive one-off clips, not hundreds of chunks that all need to sound
    # like the same narrator, so the app-level defaults passed in via
    # job.json (routes/convert.py, routes/preview.py) are tightened well
    # below these. These three remain the fallback only if job.json omits
    # the keys (e.g. a manual/direct invocation of this script).
    temperature = job.get("temperature", 1.0)
    top_p = job.get("top_p", 0.95)
    top_k = job.get("top_k", 50)

    result = {
        "engine": "higgs",
        "device_used": None,
        "chunks_total": len(chunks),
        "chunks_processed": 0,
        "errors": [],
        "safety_abort": False,
        "config": {"temperature": temperature, "top_p": top_p, "top_k": top_k},
    }

    stage, heartbeat_stop = _heartbeat.start(f"loading model (device={requested_device})")

    try:
        import torch
        from transformers import AutoProcessor, HiggsAudioV2ForConditionalGeneration

        devices_to_try = [requested_device] if requested_device == "cpu" else [requested_device, "cpu"]
        processor = model = None
        for device in devices_to_try:
            try:
                processor = AutoProcessor.from_pretrained(MODEL_ID, device_map=device)
                model = HiggsAudioV2ForConditionalGeneration.from_pretrained(
                    MODEL_ID, device_map=device,
                    dtype=torch.bfloat16 if device != "cpu" else torch.float32,
                )
                result["device_used"] = device
                break
            except Exception as e:  # noqa: BLE001
                print(f"[higgs] device {device!r} failed to load: {type(e).__name__}: {e}", file=sys.stderr)
                processor = model = None

        if model is None:
            result["errors"].append("Model failed to load on all attempted devices")
            return 1

        for i, chunk_text in enumerate(chunks):
            global_i = chunk_indices[i]
            stage["text"] = f"generating chunk {i + 1}/{len(chunks)} (global #{global_i})"
            chunk_temperature, chunk_top_p, chunk_top_k = temperature, top_p, top_k
            if chunk_params is not None:
                overrides = chunk_params[i] or {}
                chunk_temperature = float(overrides.get("temperature", temperature))
                chunk_top_p       = float(overrides.get("top_p", top_p))
                chunk_top_k       = int(overrides.get("top_k", top_k))
            try:
                conversation = build_conversation(reference_wav, chunk_text)
                inputs = processor.apply_chat_template(
                    conversation, add_generation_prompt=True, tokenize=True,
                    return_dict=True, sampling_rate=24000, return_tensors="pt",
                ).to(model.device)
                outputs = model.generate(
                    **inputs, max_new_tokens=4000, do_sample=True,
                    temperature=chunk_temperature, top_p=chunk_top_p, top_k=chunk_top_k,
                )
                decoded = processor.batch_decode(outputs)
                processor.save_audio(decoded, str(out_dir / f"chunk_{global_i:04d}.wav"))
                result["chunks_processed"] += 1
                _heartbeat.emit(f"{PROGRESS_PREFIX} {i + 1} {len(chunks)}")

                del inputs, outputs, decoded
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
                print(f"[higgs] chunk {global_i} FAILED: {e}", file=sys.stderr)

        return 0
    finally:
        heartbeat_stop.set()
        (out_dir / "results.json").write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    sys.exit(main())
