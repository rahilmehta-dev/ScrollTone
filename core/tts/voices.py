"""
Voice catalogue and character-to-voice assignment.

Responsibilities:
- Static voice pools (female / male)
- VoiceMapper: assigns consistent, gender-matched voices to named characters
"""

FEMALE_VOICES = [
    "af_heart", "af_bella", "af_nicole", "af_sarah", "af_sky",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
]
MALE_VOICES = [
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam",
    "am_michael", "am_onyx",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
]


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
