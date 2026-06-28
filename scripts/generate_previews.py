#!/usr/bin/env python3
"""
Pre-generate voice preview audio files for all Kokoro voices.
Run once at Docker build time — output goes to /app/previews/.
"""
import os
import sys
import numpy as np
import soundfile as sf
from kokoro import KPipeline

PREVIEW_DIR = "/app/previews"
os.makedirs(PREVIEW_DIR, exist_ok=True)

PREVIEW_TEXT = (
    "Hello! I'll be your narrator for this audiobook. "
    "Whether the story is long or short, I'm here to bring every page to life."
)

# voice -> lang_code mapping
VOICES = {
    "a": [
        "af_heart", "af_bella", "af_nicole", "af_sarah", "af_sky",
        "am_adam",  "am_echo",  "am_eric",   "am_fenrir",
        "am_liam",  "am_michael", "am_onyx",
    ],
    "b": [
        "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
        "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
    ],
}

pipelines = {}
ok = 0
fail = 0

for lang, voice_list in VOICES.items():
    print(f"\nLoading pipeline for lang={lang}…")
    try:
        pipelines[lang] = KPipeline(lang_code=lang, repo_id="hexgrad/Kokoro-82M")
    except Exception as e:
        print(f"  Could not load lang={lang}: {e}")
        continue

    for voice in voice_list:
        out_path = os.path.join(PREVIEW_DIR, f"{voice}.wav")
        if os.path.exists(out_path):
            print(f"  ✓ {voice} (cached)")
            ok += 1
            continue
        try:
            chunks = [audio for _, _, audio in
                      pipelines[lang](PREVIEW_TEXT, voice=voice, speed=1.0)]
            if not chunks:
                raise ValueError("empty output")
            sf.write(out_path, np.concatenate(chunks), 24000)
            print(f"  ✓ {voice}")
            ok += 1
        except Exception as e:
            print(f"  ✗ {voice}: {e}")
            fail += 1

print(f"\nDone — {ok} generated, {fail} failed.")
sys.exit(0 if fail == 0 else 1)
