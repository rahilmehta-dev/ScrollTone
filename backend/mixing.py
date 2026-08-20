"""
Ambience bed loading, scene-proportional placement, ducking, and loudness
normalization — the Phase 4 "mix ambience under narration" step.

Design notes / limitations (documented, not silent):
- Cue offsets from ``backend.ambience.detect_ambience_cues`` are character
  offsets into the ORIGINAL chapter text, but the final chapter audio is
  built from re-chunked, re-synthesized speech — there is no word-level
  forced alignment anywhere in this pipeline. Rather than fabricate
  precision that doesn't exist, a cue's audio position is approximated by
  mapping its fractional position in the source text (offset / len(text))
  to the same fractional position in the chapter's audio duration. This is
  scene-level placement, not word-level.
- Ambient beds are pre-normalized, exactly-loopable clips (see
  scripts/generate_ambience.py); tiling one needs no crossfade. A crossfade
  IS applied where the ambience *category* changes mid-chapter, so switching
  from e.g. "rain" to "crowd" doesn't cut audibly.
"""
import numpy as np

import backend.state as state

AMBIENCE_DIR = state.BASE_DIR / "backend" / "assets" / "ambience"

CUE_CONFIDENCE_THRESHOLD = 0.55     # cues below this are logged but not mixed in
AMBIENCE_BED_GAIN_DB     = -22.0    # ambience peak level under narration, before ducking
AMBIENCE_DUCK_DB         = -10.0    # extra attenuation where narration is loud
CROSSFADE_SEC            = 2.0      # crossfade when the ambience category changes mid-chapter
TARGET_RMS_DBFS          = -20.0    # loudness-normalization target, applied to every chapter

_bed_cache: dict[str, np.ndarray] = {}


def _load_bed(cue: str) -> np.ndarray | None:
    if cue in _bed_cache:
        return _bed_cache[cue]
    path = AMBIENCE_DIR / f"{cue}.wav"
    if not path.exists():
        return None
    import soundfile as sf
    audio, _sr = sf.read(str(path), dtype="float32", always_2d=False)
    _bed_cache[cue] = audio
    return audio


def _tile_to_length(bed: np.ndarray, length: int) -> np.ndarray:
    if length <= 0 or len(bed) == 0:
        return np.zeros(max(length, 0), dtype=np.float32)
    reps = length // len(bed) + 1
    return np.tile(bed, reps)[:length]


def _db_to_gain(db: float) -> float:
    return float(10 ** (db / 20))


def build_ambience_track(
    cues: list[dict], text_len: int, num_samples: int, sample_rate: int
) -> np.ndarray | None:
    """Build one continuous ambience bed spanning a chapter's audio.

    Cues below CUE_CONFIDENCE_THRESHOLD (or naming a category with no
    bundled clip) are dropped. Returns None if no cue survives — the
    chapter plays narration-only, same as ambience being off.
    """
    usable = sorted(
        (c for c in cues if c["confidence"] >= CUE_CONFIDENCE_THRESHOLD and _load_bed(c["cue"]) is not None),
        key=lambda c: c["start_offset"],
    )
    if not usable or num_samples <= 0:
        return None

    boundaries = [
        int(min(max(c["start_offset"] / max(text_len, 1), 0.0), 1.0) * num_samples)
        for c in usable
    ]
    boundaries.append(num_samples)

    track = np.zeros(num_samples, dtype=np.float32)
    crossfade_n = min(int(CROSSFADE_SEC * sample_rate), num_samples // 4 or 1)

    for i, cue in enumerate(usable):
        seg_start, seg_end = boundaries[i], boundaries[i + 1]
        if seg_end <= seg_start:
            continue

        write_start = seg_start
        fade_in_len = 0
        if i > 0 and usable[i - 1]["cue"] != cue["cue"]:
            fade_in_len = min(crossfade_n, seg_end - seg_start, seg_start)
            write_start = seg_start - fade_in_len

        bed = _tile_to_length(_load_bed(cue["cue"]), seg_end - write_start).astype(np.float32).copy()

        if fade_in_len > 0:
            fade_in = np.linspace(0.0, 1.0, fade_in_len, dtype=np.float32)
            bed[:fade_in_len] *= fade_in
            # fade out whatever the previous segment already wrote over the same overlap
            track[write_start:seg_start] *= (1.0 - fade_in)

        track[write_start:seg_end] += bed

    return track


def _moving_average(x: np.ndarray, window: int) -> np.ndarray:
    """O(n) box-filter smoothing via cumulative sum (no scipy dependency)."""
    if window <= 1 or len(x) == 0:
        return x
    window = min(window, len(x))
    cumsum = np.cumsum(np.insert(x, 0, 0.0))
    ma = (cumsum[window:] - cumsum[:-window]) / window
    pad_left = window // 2
    pad_right = len(x) - len(ma) - pad_left
    return np.pad(ma, (pad_left, max(pad_right, 0)))[:len(x)]


def mix_ambience_under_narration(
    narration: np.ndarray, ambience: np.ndarray, sample_rate: int
) -> np.ndarray:
    """Overlay *ambience* under *narration*, ducked below narration energy.

    Ducking uses a smoothed envelope follower on the narration — not a true
    sidechain compressor, but enough to keep the bed from competing with
    speech (see AMBIENCE_DUCK_DB / AMBIENCE_BED_GAIN_DB above). Final mix is
    peak-limited so ambience can never push the file into clipping.
    """
    if len(ambience) < len(narration):
        ambience = np.pad(ambience, (0, len(narration) - len(ambience)))
    else:
        ambience = ambience[:len(narration)]

    envelope = _moving_average(np.abs(narration), window=int(0.3 * sample_rate))
    peak = float(np.max(envelope)) if len(envelope) else 0.0
    norm_env = envelope / peak if peak > 1e-9 else envelope

    base_gain = _db_to_gain(AMBIENCE_BED_GAIN_DB)
    duck_gain = _db_to_gain(AMBIENCE_DUCK_DB)
    gain_curve = base_gain * (1.0 - norm_env * (1.0 - duck_gain))

    mixed = narration + ambience * gain_curve

    mix_peak = float(np.max(np.abs(mixed))) if len(mixed) else 0.0
    if mix_peak > 0.98:
        mixed = mixed * (0.98 / mix_peak)
    return mixed.astype(np.float32)


def normalize_loudness(audio: np.ndarray, target_dbfs: float = TARGET_RMS_DBFS) -> np.ndarray:
    """RMS-based loudness normalization, applied to every chapter (ambience
    on or off) so chapters don't vary wildly in perceived volume.

    Not true LUFS/EBU R128 metering — that would need a new dependency
    (e.g. pyloudnorm), which the "fully local, lightweight" design goal
    calls out as something to flag rather than add silently, so this is a
    deliberately lighter RMS approximation instead. Still enough to even
    out gross level differences between chapters.
    """
    if len(audio) == 0:
        return audio
    rms = float(np.sqrt(np.mean(np.square(audio))))
    if rms < 1e-9:
        return audio
    gain = _db_to_gain(target_dbfs) / rms
    out = audio * gain
    peak = float(np.max(np.abs(out)))
    if peak > 0.98:
        out = out * (0.98 / peak)
    return out.astype(np.float32)
