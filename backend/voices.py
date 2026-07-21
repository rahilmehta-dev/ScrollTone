"""
Voice catalogue, character-to-voice assignment, and preview generation.

Responsibilities:
- Static voice pools (female / male) and the known-voice set
- VoiceMapper: assigns consistent, gender-matched voices to named characters
- On-demand preview clip generation (lazy, cached per language)
"""
from pathlib import Path

import backend.state as state

FEMALE_VOICES = [
    "af_heart", "af_bella", "af_nicole", "af_sarah", "af_sky",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
]
MALE_VOICES = [
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam",
    "am_michael", "am_onyx",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
]
KNOWN_VOICES = {
    "af_heart", "af_bella", "af_nicole", "af_sarah", "af_sky",
    "am_adam",  "am_echo",  "am_eric",   "am_fenrir",
    "am_liam",  "am_michael", "am_onyx",
    "bf_alice", "bf_emma",  "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
}


class VoiceMapper:
    """Assigns consistent, gender-matched voices to characters across chapters."""

    def __init__(self, narrator_voice: str):
        self.narrator_voice = narrator_voice
        self._map: dict[str, str] = {}
        self._female_pool = [v for v in FEMALE_VOICES if v != narrator_voice]
        self._male_pool   = [v for v in MALE_VOICES   if v != narrator_voice]
        self._female_idx  = 0
        self._male_idx    = 0

    def get_voice(self, speaker: str | None, gender: str | None) -> str:
        if not speaker:
            return self.narrator_voice
        key = speaker.strip().title()
        if key not in self._map:
            self._map[key] = self._assign(gender)
        return self._map[key]

    def _assign(self, gender: str | None) -> str:
        if gender == "male" and self._male_pool:
            v = self._male_pool[self._male_idx % len(self._male_pool)]
            self._male_idx += 1
            return v
        if gender == "female" and self._female_pool:
            v = self._female_pool[self._female_idx % len(self._female_pool)]
            self._female_idx += 1
            return v
        # Unknown — alternate pools
        if self._female_idx <= self._male_idx and self._female_pool:
            v = self._female_pool[self._female_idx % len(self._female_pool)]
            self._female_idx += 1
            return v
        if self._male_pool:
            v = self._male_pool[self._male_idx % len(self._male_pool)]
            self._male_idx += 1
            return v
        return self.narrator_voice

    def summary(self) -> str:
        if not self._map:
            return "(no characters detected yet)"
        return "  |  ".join(f"{n} → {v}" for n, v in self._map.items())


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
            import torch
            if torch.backends.mps.is_available():
                _device = "mps"
            elif torch.cuda.is_available():
                _device = "cuda"
            else:
                _device = "cpu"
            state._preview_pipeline[lang] = KPipeline(
                lang_code=lang, repo_id="hexgrad/Kokoro-82M", device=_device)
        pipeline = state._preview_pipeline[lang]
        try:
            import numpy as np
            import soundfile as sf
            chunks = [a for _, _, a in pipeline(PREVIEW_TEXT, voice=voice, speed=1.0)]
            if chunks:
                sf.write(str(out_path), np.concatenate(chunks), 24000)
        except Exception as e:
            print(f"Preview generation failed for {voice}: {e}", flush=True)
