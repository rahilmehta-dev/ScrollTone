"""
Kokoro TTS engine utilities.

Responsibilities:
- Voice catalogue and preview text constants
- On-demand preview clip generation (lazy, cached per language)
"""
from pathlib import Path
import core.state as state

KNOWN_VOICES = {
    "af_heart", "af_bella", "af_nicole", "af_sarah", "af_sky",
    "am_adam",  "am_echo",  "am_eric",   "am_fenrir",
    "am_liam",  "am_michael", "am_onyx",
    "bf_alice", "bf_emma",  "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
}

PREVIEW_TEXT = (
    "Hello! I'll be your narrator for this audiobook. "
    "Whether the story is long or short, I'm here to bring every page to life."
)


def _generate_preview(voice: str, out_path: Path) -> None:
    """Synthesize a short preview clip for *voice* and write it to *out_path*.

    Uses a per-language pipeline cache so each language model is loaded only
    once per process lifetime.  Protected by state._preview_lock so concurrent
    requests for the same language don't double-load the model.
    """
    lang = "b" if voice[:2] in ("bf", "bm") else "a"
    with state._preview_lock:
        if lang not in state._preview_pipeline:
            from kokoro import KPipeline
            state._preview_pipeline[lang] = KPipeline(
                lang_code=lang, repo_id="hexgrad/Kokoro-82M")
        pipeline = state._preview_pipeline[lang]
        try:
            import numpy as np
            import soundfile as sf
            chunks = [a for _, _, a in pipeline(PREVIEW_TEXT, voice=voice, speed=1.0)]
            if chunks:
                sf.write(str(out_path), np.concatenate(chunks), 24000)
        except Exception as e:
            print(f"Preview generation failed for {voice}: {e}", flush=True)
