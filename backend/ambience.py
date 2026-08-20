"""
Ambient scene-cue detection via the local Ollama LLM.

Tags a chapter's text with ambient background categories (weather, setting)
where the text clearly and explicitly supports it — conservative by design,
so silence/no-tag is the default for anything ambiguous.

The LLM is asked to quote a short verbatim excerpt marking where each cue
starts, rather than to compute a character offset itself (small local models
are unreliable at arithmetic on long text) — the offset is then found
deterministically in code via ``str.find`` on that excerpt.
"""

import re

from backend.attribution import ask_ollama

# Categories are deliberately limited to the ambient loops actually bundled
# in backend/assets/ambience/ (see Phase 3 / AMBIENCE_SOUNDS.md) — asking the
# LLM to only choose from a fixed, backed vocabulary avoids ever landing on a
# cue with no audio to play.
AMBIENCE_CATEGORIES = ["rain", "wind", "ocean", "fire", "forest", "crowd"]

_MAX_CUES_PER_CHAPTER = 3
_MIN_CONFIDENCE       = 0.0   # caller (mixing) applies its own threshold; keep all here for debugging


def _parse_cue_lines(raw: str, chapter_text: str) -> list[dict]:
    cues = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line or line.upper() == "NONE":
            continue
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        category, confidence_str, quote = (p.strip() for p in parts)
        category = category.lower()
        if category not in AMBIENCE_CATEGORIES:
            continue
        try:
            confidence = max(0.0, min(1.0, float(confidence_str)))
        except ValueError:
            confidence = 0.5

        quote = quote.strip(' "“”')
        if not quote:
            continue
        offset = chapter_text.find(quote)
        if offset < 0:
            # LLM sometimes paraphrases slightly — try a shorter prefix before giving up.
            shorter = quote[:max(8, len(quote) // 2)]
            offset = chapter_text.find(shorter)
        if offset < 0:
            continue   # can't verify the quote actually occurs — drop rather than guess

        cues.append({"start_offset": offset, "cue": category, "confidence": confidence})

    cues.sort(key=lambda c: c["start_offset"])
    return cues[:_MAX_CUES_PER_CHAPTER]


def detect_ambience_cues(
    text: str,
    ollama_url: str,
    model: str,
    timeout: int = 120,
) -> list[dict]:
    """Return conservative ambient-scene cues for *text* (one chapter).

    Returns a list of ``{"start_offset": int, "cue": str, "confidence": float}``,
    sorted by offset, capped at a few per chapter. Empty list if nothing
    clearly qualifies.

    Raises ``urllib.error.URLError`` on connection problems.
    """
    prompt = (
        "Below is a chapter from a novel. Identify moments where the text CLEARLY and "
        "explicitly establishes one of these ambient background sounds:\n"
        + ", ".join(AMBIENCE_CATEGORIES) + ".\n\n"
        "Be conservative: only tag a category if the text actually describes that sound "
        "happening (e.g. rain literally falling, wind literally blowing, waves/sea, a fire "
        "burning, a forest/woods setting, or a crowd of people). Do not tag based on mood, "
        "metaphor, or a single passing word. Skip anything ambiguous.\n\n"
        "For each moment found, respond with ONE line in exactly this format:\n"
        "  category|confidence|quote\n"
        "where confidence is a number from 0 to 1, and quote is a short (5-12 word) excerpt "
        "copied VERBATIM from the text marking where that ambience begins.\n"
        "If nothing clearly qualifies, respond with exactly: NONE\n"
        "Do not invent categories outside the list above. Output at most 3 lines.\n\n"
        f"CHAPTER TEXT:\n{text[:4000]}"
    )

    raw = ask_ollama(
        {
            "model":    model,
            "messages": [{"role": "user", "content": prompt}],
            "stream":   False,
            "options":  {"temperature": 0},
        },
        ollama_url,
        timeout,
    )
    return _parse_cue_lines(raw, text)
