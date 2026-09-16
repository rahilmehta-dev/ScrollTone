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
- Bundled beds are a mix of sources now (see backend/assets/ambience/ATTRIBUTION.md):
  some are procedurally synthesized (scripts/generate_ambience.py) and
  already exactly loopable by construction; others are real recordings
  (scripts/fetch_ambience.py) that were never authored to tile perfectly.
  Rather than trust every bed's peak level and loop point, both are handled
  defensively at load time: _load_bed peak-normalizes so a quiet clock
  recording and a loud fire recording sit at the same nominal level, and
  _tile_to_length crossfades its own seam so a real recording's start/end
  mismatch doesn't click on every repeat (a no-op for the synthesized beds,
  which already match at the seam). A separate crossfade is applied where
  the ambience *category* changes mid-chapter, so switching from e.g.
  "rain" to "crowd" doesn't cut audibly.
"""
import numpy as np

import backend.state as state

AMBIENCE_DIR = state.BASE_DIR / "backend" / "assets" / "ambience"

CUE_CONFIDENCE_THRESHOLD = 0.55     # cues below this are logged but not mixed in
AMBIENCE_BED_GAIN_DB     = -24.0    # ambience peak level under narration, before ducking
AMBIENCE_DUCK_DB         = -18.0    # extra attenuation where narration is loud
CROSSFADE_SEC            = 2.0      # crossfade when the ambience category changes mid-chapter
TARGET_RMS_DBFS          = -20.0    # loudness-normalization target, applied to every chapter

# Ducking (an amplitude envelope) can't by itself stop a broadband bed like
# rain/crowd hiss from sitting right on top of consonants and sibilance --
# both occupy the same 250 Hz-4 kHz band that carries speech intelligibility,
# so no amount of volume reduction alone keeps the two from smearing
# together. Carving that band out of the ambience (never the narration)
# leaves room for the voice regardless of how loud the bed's peak envelope
# gets. See _carve_vocal_band / mix_ambience_under_narration.
VOCAL_BAND_LOW_HZ        = 250.0    # low edge of the carved range
VOCAL_BAND_HIGH_HZ       = 4000.0   # high edge -- covers fundamental + presence/sibilance
VOCAL_BAND_ATTEN_DB      = -9.0     # attenuation applied to ambience inside that range
VOCAL_BAND_TAPER_HZ      = 300.0    # width of the smooth transition at each edge (avoids ringing)

BED_PEAK_TARGET          = 0.5      # every bed is normalized to this peak on load (see _load_bed)
LOOP_SEAM_CROSSFADE_SEC  = 0.1      # tile-seam crossfade width (see _tile_to_length)

_bed_cache: dict[str, np.ndarray] = {}


def _load_bed(cue: str) -> np.ndarray | None:
    """Load and cache the bed for *cue*, peak-normalized to BED_PEAK_TARGET.

    Bundled beds come from different sources at different original levels
    (a synthesized loop vs. a real recording pulled in as-is) -- normalizing
    here means AMBIENCE_BED_GAIN_DB means the same thing for every category,
    rather than some being quietly louder or softer than others by accident.
    """
    if cue in _bed_cache:
        return _bed_cache[cue]
    path = AMBIENCE_DIR / f"{cue}.wav"
    if not path.exists():
        return None
    import soundfile as sf
    audio, _sr = sf.read(str(path), dtype="float32", always_2d=False)
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    if peak > 1e-9:
        audio = audio * (BED_PEAK_TARGET / peak)
    _bed_cache[cue] = audio
    return audio


def _tile_to_length(bed: np.ndarray, length: int, sample_rate: int) -> np.ndarray:
    """Tile *bed* to *length* samples, crossfading the loop seam.

    A bed built to be exactly periodic (the synthesized ones) already
    matches at its own seam, so this crossfade changes nothing audible for
    those. A real recording generally isn't -- its last sample and first
    sample don't match -- so without this, every repeat would click. The
    fix is the standard loop-crossfade trick: blend the head of the loop
    with its tail so the transition is spread across a short window instead
    of concentrated at one sample, then tile that.
    """
    if length <= 0 or len(bed) == 0:
        return np.zeros(max(length, 0), dtype=np.float32)
    if length <= len(bed):
        return bed[:length].astype(np.float32).copy()

    fade_n = min(int(LOOP_SEAM_CROSSFADE_SEC * sample_rate), len(bed) // 4 or 1)
    if fade_n > 1:
        fade = np.linspace(0.0, 1.0, fade_n, dtype=np.float32)
        # Blend the tail into the head AND drop the tail, shortening the loop
        # by fade_n. Dropping it is the part that actually removes the click:
        # keeping the full-length bed leaves its original last sample sitting
        # right before its (now tail-blended) first sample, so the end->start
        # discontinuity survives — just relocated, not removed.
        seamless = bed[:len(bed) - fade_n].astype(np.float32).copy()
        seamless[:fade_n] = bed[:fade_n] * fade + bed[-fade_n:] * (1.0 - fade)
    else:
        seamless = bed.astype(np.float32).copy()

    reps = length // len(seamless) + 1
    return np.tile(seamless, reps)[:length]


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

        bed = _tile_to_length(_load_bed(cue["cue"]), seg_end - write_start, sample_rate)

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


def _carve_vocal_band(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    """Attenuate *audio* inside the speech-intelligibility band (see
    VOCAL_BAND_* above), leaving everything outside it untouched.

    A single FFT/IFFT over the whole track (not a per-window filter) is
    enough since this is a static spectral shape, independent of the
    narration's content -- it only needs computing once per chapter.
    """
    if len(audio) == 0:
        return audio
    n = len(audio)
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
    atten = _db_to_gain(VOCAL_BAND_ATTEN_DB)

    gain = np.ones_like(freqs)
    lo_start, lo_end = VOCAL_BAND_LOW_HZ - VOCAL_BAND_TAPER_HZ, VOCAL_BAND_LOW_HZ
    hi_start, hi_end = VOCAL_BAND_HIGH_HZ, VOCAL_BAND_HIGH_HZ + VOCAL_BAND_TAPER_HZ

    gain[(freqs >= lo_end) & (freqs <= hi_start)] = atten

    fade_in = (freqs >= lo_start) & (freqs < lo_end)
    gain[fade_in] = 1.0 + (atten - 1.0) * (freqs[fade_in] - lo_start) / VOCAL_BAND_TAPER_HZ

    fade_out = (freqs > hi_start) & (freqs <= hi_end)
    gain[fade_out] = atten + (1.0 - atten) * (freqs[fade_out] - hi_start) / VOCAL_BAND_TAPER_HZ

    carved = np.fft.irfft(np.fft.rfft(audio) * gain, n=n)
    return carved.astype(np.float32)


def mix_ambience_under_narration(
    narration: np.ndarray, ambience: np.ndarray, sample_rate: int
) -> np.ndarray:
    """Overlay *ambience* under *narration*, ducked below narration energy.

    Two independent moves keep the bed from clashing with speech:
    - a spectral carve (_carve_vocal_band) removes ambience energy from the
      band speech intelligibility depends on, regardless of loudness;
    - a smoothed envelope follower on the narration then ducks the bed's
      overall level further while narration is loud (not a true sidechain
      compressor, but enough on top of the carve -- see AMBIENCE_DUCK_DB /
      AMBIENCE_BED_GAIN_DB above).
    Final mix is peak-limited so ambience can never push the file into
    clipping.
    """
    if len(ambience) < len(narration):
        ambience = np.pad(ambience, (0, len(narration) - len(ambience)))
    else:
        ambience = ambience[:len(narration)]

    ambience = _carve_vocal_band(ambience, sample_rate)

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
