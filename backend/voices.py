"""
Voice catalogue, character-to-voice assignment, and preview generation.

Responsibilities:
- Static voice pools (female / male) and the known-voice set
- VoiceMapper: assigns consistent, gender-matched voices to named characters,
  and persists that assignment to disk so it survives across separate runs
- On-demand preview clip generation (lazy, cached per language)
"""
import json
from pathlib import Path

import backend.state as state

REGISTRY_FILENAME = "character_voices.json"

# Ordered best-grade-first per the official Kokoro-82M quality grades
# (huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md) so round-robin
# character assignment in VoiceMapper hands out the most natural-sounding
# voices before falling back to weaker ones.
FEMALE_VOICES = [
    "af_heart", "af_bella",                      # A, A-
    "af_nicole", "bf_emma",                      # B-
    "af_aoede", "af_kore", "af_sarah",            # C+
    "bf_isabella",                                # C
    "af_sky",                                     # C-
    "bf_alice", "bf_lily",                        # D
]
MALE_VOICES = [
    "am_fenrir", "am_michael", "am_puck",         # C+ (best available grade for male voices)
    "bm_fable", "bm_george",                      # C
    "bm_lewis",                                   # D+
    "am_echo", "am_eric", "am_liam", "am_onyx", "bm_daniel",  # D
    "am_adam",                                    # F+
]
KNOWN_VOICES = set(FEMALE_VOICES) | set(MALE_VOICES)


class VoiceMapper:
    """Assigns consistent, gender-matched voices to characters across chapters.

    The name -> voice map (plus round-robin pool indices) can be persisted to
    a JSON file so that re-running generation later — e.g. a partial re-run
    for just the later chapters of a book — reuses the same voice for a
    character instead of reassigning it from scratch.
    """

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

    def known_names(self) -> list[str]:
        """Characters already assigned a voice, for LLM re-identification prompts."""
        return list(self._map.keys())

    def load(self, registry_path: Path) -> None:
        """Seed the map + pool indices from a previous run's registry, if any.

        Skipped (silently, starting fresh) if the file is missing, unreadable,
        or was written for a different narrator voice — mixing pool indices
        across different narrator exclusions could hand out the narrator's
        own voice to a character.
        """
        if not registry_path.exists():
            return
        try:
            data = json.loads(registry_path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if data.get("narrator_voice") != self.narrator_voice:
            return
        self._map        = dict(data.get("map", {}))
        self._female_idx = int(data.get("female_idx", 0))
        self._male_idx   = int(data.get("male_idx", 0))

    def save(self, registry_path: Path) -> None:
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text(json.dumps({
            "narrator_voice": self.narrator_voice,
            "map":            self._map,
            "female_idx":     self._female_idx,
            "male_idx":       self._male_idx,
        }, indent=2))

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
