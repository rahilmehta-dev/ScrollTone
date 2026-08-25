"""Standalone Higgs Audio V2 synthesis worker.

Not part of the main app's import graph — invoked as a subprocess via the
`.venv-higgs` interpreter (backend/pipeline.py launches it per chapter), so it
can depend on transformers/torch without those being installed in the main
ScrollTone venv. See README "Optional: Higgs Audio V2 / Chatterbox engines".

Usage:
    .venv-higgs/bin/python backend/engines/higgs_synth.py <job.json>

job.json:
    {"chunks": [str, ...], "reference_wav": str, "out_dir": str, "device": "mps"|"cpu"|"cuda"}

Writes chunk_0000.wav, chunk_0001.wav, ... (one per successfully synthesized
chunk, gaps on failure) into out_dir, plus results.json summarizing the run.
Progress is reported on stdout as lines matching PROGRESS_PREFIX, which the
parent process greps for — everything else on stdout/stderr (model-loading
logs, tqdm bars, transformers warnings) is noise the parent ignores.
"""
import gc
import json
import resource
import sys
from pathlib import Path

MODEL_ID = "bosonai/higgs-audio-v2-generation-3B-base"
PROGRESS_PREFIX = "##PROGRESS##"
SAFE_RSS_LIMIT_GB = 24.0  # defense-in-depth; Higgs peaked ~12GB in testing

SCENE_PROMPT = "Audio is recorded from a quiet room."


def rss_gb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024.0 ** 3)


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

    # Boson AI's own documented production defaults; overridable via job.json
    # for experimentation (e.g. lower temperature for more voice-consistent
    # output across chunks, at some cost to expressiveness).
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
        (out_dir / "results.json").write_text(json.dumps(result, indent=2))
        return 1

    for i, chunk_text in enumerate(chunks):
        try:
            conversation = build_conversation(reference_wav, chunk_text)
            inputs = processor.apply_chat_template(
                conversation, add_generation_prompt=True, tokenize=True,
                return_dict=True, sampling_rate=24000, return_tensors="pt",
            ).to(model.device)
            outputs = model.generate(
                **inputs, max_new_tokens=4000, do_sample=True,
                temperature=temperature, top_p=top_p, top_k=top_k,
            )
            decoded = processor.batch_decode(outputs)
            processor.save_audio(decoded, str(out_dir / f"chunk_{i:04d}.wav"))
            result["chunks_processed"] += 1
            print(f"{PROGRESS_PREFIX} {i + 1} {len(chunks)}", flush=True)

            del inputs, outputs, decoded
            gc.collect()

            current_rss = rss_gb()
            if current_rss > SAFE_RSS_LIMIT_GB:
                result["errors"].append(
                    f"Safety abort: RSS hit {current_rss:.1f}GB (limit {SAFE_RSS_LIMIT_GB}GB) "
                    f"after chunk {i + 1}/{len(chunks)}"
                )
                result["safety_abort"] = True
                break
        except Exception as e:  # noqa: BLE001
            result["errors"].append(f"chunk {i}: {type(e).__name__}: {e}")
            print(f"[higgs] chunk {i} FAILED: {e}", file=sys.stderr)

    (out_dir / "results.json").write_text(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
