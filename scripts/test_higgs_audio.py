"""Feasibility test for Higgs Audio V2 on this machine (Apple M1 Max, no CUDA).

Standalone script — run with the isolated .venv-higgs environment, NOT the
project's main .venv (which is Kokoro's). This does not touch backend/ code.

    .venv-higgs/bin/python scripts/test_higgs_audio.py

Tries device_map="mps" first, falls back to "cpu" on failure. Uses
previews/af_bella.wav (an existing Kokoro-generated clip) as the zero-shot
voice-cloning reference. Reports load time, generation time, and RTF.
"""

import sys
import time
from pathlib import Path

MODEL_ID = "bosonai/higgs-audio-v2-generation-3B-base"
REFERENCE_WAV = str(Path(__file__).resolve().parent.parent / "previews" / "af_bella.wav")
TEXT = "Hello, I'll be your narrator for this audiobook."
OUT_WAV = str(Path(__file__).resolve().parent.parent / "test_higgs_output.wav")


def build_conversation():
    return [
        {
            "role": "system",
            "content": [{"type": "text", "text": "Generate audio following instruction."}],
        },
        {
            "role": "scene",
            "content": [{"type": "text", "text": "Audio is recorded from a quiet room."}],
        },
        {
            "role": "user",
            "content": [{"type": "text", "text": "This is a short reference clip of my narrator voice."}],
        },
        {
            "role": "assistant",
            "content": [{"type": "audio", "path": REFERENCE_WAV}],
        },
        {
            "role": "user",
            "content": [{"type": "text", "text": TEXT}],
        },
    ]


def try_device(device):
    import torch
    from transformers import AutoProcessor, HiggsAudioV2ForConditionalGeneration

    print(f"\n=== Attempting device_map={device!r} ===")
    t0 = time.time()
    processor = AutoProcessor.from_pretrained(MODEL_ID, device_map=device)
    model = HiggsAudioV2ForConditionalGeneration.from_pretrained(
        MODEL_ID, device_map=device, dtype=torch.bfloat16 if device != "cpu" else torch.float32
    )
    load_time = time.time() - t0
    print(f"Model+processor load time: {load_time:.1f}s")

    conversation = build_conversation()
    inputs = processor.apply_chat_template(
        conversation,
        add_generation_prompt=True,
        tokenize=True,
        return_dict=True,
        sampling_rate=24000,
        return_tensors="pt",
    ).to(model.device)

    t0 = time.time()
    outputs = model.generate(**inputs, max_new_tokens=500, do_sample=False)
    gen_time = time.time() - t0
    print(f"Generation time: {gen_time:.1f}s")

    decoded = processor.batch_decode(outputs)
    processor.save_audio(decoded, OUT_WAV)

    import soundfile as sf

    info = sf.info(OUT_WAV)
    rtf = info.duration / gen_time if gen_time > 0 else float("nan")
    print(f"Output audio duration: {info.duration:.2f}s")
    print(f"Real-time factor (audio_seconds / wall_seconds): {rtf:.2f}x")
    print(f"Saved to: {OUT_WAV}")
    return True


def main():
    for device in ("mps", "cpu"):
        try:
            if try_device(device):
                return 0
        except Exception as e:  # noqa: BLE001
            print(f"FAILED on device_map={device!r}: {type(e).__name__}: {e}")
            import traceback

            traceback.print_exc()
    print("\nAll device attempts failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
