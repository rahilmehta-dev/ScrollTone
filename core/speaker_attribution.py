"""
Speaker attribution via local Ollama LLM — hybrid approach.

Step 1 — Regex splits the text into narration vs quoted-dialogue segments.
          This is reliable and needs no LLM.

Step 2 — LLM is asked ONE simple question per chapter:
          "For each numbered quote, who is the speaker and what gender?"
          Small models handle this well; they just answer a list, no JSON
          restructuring required.

Step 3 — Attributions are merged back into the segments for multi-voice TTS.
"""

import json
import re
import urllib.request
import urllib.error


# ── Voice pools ───────────────────────────────────────────────────────────────
FEMALE_VOICES = [
    "af_heart", "af_bella", "af_nicole", "af_sarah", "af_sky",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
]
MALE_VOICES = [
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam",
    "am_michael", "am_onyx",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
]

# Matches both straight "..." and curly "..." quotes; min 4 chars inside
_QUOTE_RE = re.compile(r'[\u201c"](.{4,}?)[\u201d"]', re.DOTALL)


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


# ── Step 1: regex dialogue split ──────────────────────────────────────────────

def _regex_split(text: str) -> list[dict]:
    """Split *text* into narration/dialogue segments using quote detection."""
    segments = []
    last_end = 0

    for m in _QUOTE_RE.finditer(text):
        before = text[last_end:m.start()].strip()
        if before:
            segments.append({"type": "narration", "text": before,
                              "speaker": None, "gender": None})
        dialogue_text = m.group(1).strip()
        if dialogue_text:
            segments.append({"type": "dialogue", "text": dialogue_text,
                              "speaker": None, "gender": None})
        last_end = m.end()

    tail = text[last_end:].strip()
    if tail:
        segments.append({"type": "narration", "text": tail,
                          "speaker": None, "gender": None})

    if not segments:
        segments = [{"type": "narration", "text": text,
                     "speaker": None, "gender": None}]
    return segments


# ── Step 2: LLM attribution ───────────────────────────────────────────────────

def _ask_ollama(payload: dict, ollama_url: str, timeout: int) -> str:
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        ollama_url.rstrip("/") + "/api/chat",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())["message"]["content"]


def _ask_attributions(
    full_text: str,
    dialogue_indices: list[int],
    segments: list[dict],
    ollama_url: str,
    model: str,
    timeout: int,
) -> list[dict]:
    """Ask the LLM who speaks each numbered dialogue line."""
    numbered = []
    for i, seg_i in enumerate(dialogue_indices):
        preview = segments[seg_i]["text"][:100].replace("\n", " ")
        numbered.append(f'{i + 1}. "{preview}"')

    prompt = (
        "Below is a passage from a novel followed by a numbered list of quoted lines from it.\n"
        "For each number, identify the speaker's name and gender.\n"
        "Answer with ONLY one line per number in this exact format:\n"
        "  NUMBER. Name|male   or   NUMBER. Name|female   or   NUMBER. Unknown|unknown\n\n"
        f"PASSAGE:\n{full_text[:3000]}\n\n"
        "QUOTED LINES:\n" + "\n".join(numbered)
    )

    raw = _ask_ollama(
        {
            "model":   model,
            "messages": [{"role": "user", "content": prompt}],
            "stream":  False,
            "options": {"temperature": 0},
        },
        ollama_url,
        timeout,
    )
    return _parse_attribution_lines(raw, len(dialogue_indices))


def _parse_attribution_lines(raw: str, count: int) -> list[dict]:
    results = [{"speaker": None, "gender": None}] * count
    for line in raw.strip().splitlines():
        m = re.match(r"(\d+)[.)]\s*([^|]+)\|([a-zA-Z]+)", line.strip())
        if not m:
            continue
        idx     = int(m.group(1)) - 1
        speaker = m.group(2).strip()
        gender  = m.group(3).strip().lower()
        if idx < 0 or idx >= count:
            continue
        if speaker.lower() in ("unknown", "unattributed", "narrator", "none", ""):
            speaker = None
        results[idx] = {
            "speaker": speaker,
            "gender":  gender if gender in ("male", "female") else "unknown",
        }
    return results


# ── Public API ────────────────────────────────────────────────────────────────

def attribute_speakers(
    text: str,
    ollama_url: str,
    model: str,
    timeout: int = 90,
) -> list[dict]:
    """Split *text* into segments and identify dialogue speakers via Ollama.

    Returns list of:
        {"type": "narration"|"dialogue",
         "speaker": str|None, "gender": str|None, "text": str}

    Raises ``urllib.error.URLError`` on connection problems.
    """
    # Step 1 — structural split (no LLM needed)
    segments = _regex_split(text)

    dialogue_indices = [i for i, s in enumerate(segments) if s["type"] == "dialogue"]
    if not dialogue_indices:
        return segments   # pure narration — skip LLM entirely

    # Step 2 — ask LLM only "who said each line?"
    attributions = _ask_attributions(
        text, dialogue_indices, segments, ollama_url, model, timeout
    )

    # Step 3 — merge back
    for attr_i, seg_i in enumerate(dialogue_indices):
        segments[seg_i]["speaker"] = attributions[attr_i]["speaker"]
        segments[seg_i]["gender"]  = attributions[attr_i]["gender"]

    return segments
