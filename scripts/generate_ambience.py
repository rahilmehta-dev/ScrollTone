"""
Procedurally synthesizes the still-synthetic bundled ambient background loops.

rain/ocean/fire/clock were originally synthesized here too, but are now real
recordings pulled in by scripts/fetch_ambience.py instead (see
documentation/ambience.md for why: procedural noise was cheap to license but
sounded harsh and un-atmospheric against narration). wind/forest/crowd
remain synthesized below because no equivalently well-licensed real
recording has been sourced for them yet -- see documentation/ambience.md for
the current gap list. Running this script only ever touches the categories
in GENERATORS below; it will not overwrite the real recordings.

Every generator builds its loop directly in the frequency domain (random
phase per FFT bin, then a single inverse FFT into a fixed-length buffer).
An IFFT's basis functions are themselves periodic over that buffer length,
so the resulting time-domain signal is *exactly* periodic -- tiling it back
to back for playback produces no seam and needs no crossfade. Every later
step (adding two such signals, multiplying by an envelope built the same
way, masking frequency bins) preserves that periodicity.

Run once (output is committed to the repo, not regenerated at runtime):
    python scripts/generate_ambience.py
"""
from pathlib import Path

import numpy as np
import soundfile as sf

SAMPLE_RATE  = 24000   # matches Kokoro's output rate -- no resampling needed at mix time
DURATION_SEC = 24
N            = SAMPLE_RATE * DURATION_SEC
OUT_DIR      = Path(__file__).parent.parent / "backend" / "assets" / "ambience"

_SEEDS = {"wind": 2, "forest": 5, "crowd": 6}


# ── Building blocks (all exactly periodic over N samples) ─────────────────────

def _seamless_noise(n: int, alpha: float, rng: np.random.Generator) -> np.ndarray:
    """Colored noise with magnitude ~ 1/f**alpha and random phase per bin."""
    freqs = np.fft.rfftfreq(n, d=1.0 / SAMPLE_RATE)
    mag = np.zeros_like(freqs)
    mag[1:] = 1.0 / (freqs[1:] ** alpha)
    phase = rng.uniform(0, 2 * np.pi, size=freqs.shape)
    signal = np.fft.irfft(mag * np.exp(1j * phase), n=n)
    return _peak_normalize(signal, 1.0)


def _formant_noise(n: int, rng: np.random.Generator, centers_hz: list[float], bandwidth_hz: float) -> np.ndarray:
    """Noise shaped as a sum of gaussian bumps around *centers_hz* (murmur-like)."""
    freqs = np.fft.rfftfreq(n, d=1.0 / SAMPLE_RATE)
    mag = np.zeros_like(freqs)
    for center in centers_hz:
        mag += np.exp(-0.5 * ((freqs - center) / bandwidth_hz) ** 2)
    phase = rng.uniform(0, 2 * np.pi, size=freqs.shape)
    signal = np.fft.irfft(mag * np.exp(1j * phase), n=n)
    return _peak_normalize(signal, 1.0)


def _band_limit(signal: np.ndarray, low_hz: float, high_hz: float) -> np.ndarray:
    n = len(signal)
    freqs = np.fft.rfftfreq(n, d=1.0 / SAMPLE_RATE)
    mask = (freqs >= low_hz) & (freqs <= high_hz)
    out = np.fft.irfft(np.fft.rfft(signal) * mask, n=n)
    return _peak_normalize(out, 1.0)


def _periodic_envelope(n: int, cycles: int, rng: np.random.Generator, base: float, depth: float) -> np.ndarray:
    """A slow 0..1-ish amplitude envelope with an integer cycle count over n samples."""
    t = np.linspace(0, 2 * np.pi * cycles, n, endpoint=False)
    wave01 = (np.sin(t + rng.uniform(0, 2 * np.pi)) + 1) / 2
    return base + depth * wave01


def _chirp_kernel(rng: np.random.Generator, dur_sec: float = 0.25) -> np.ndarray:
    n = int(dur_sec * SAMPLE_RATE)
    f0, f1 = rng.uniform(1800, 2500), rng.uniform(3000, 4500)
    freq = np.linspace(f0, f1, n)
    chirp = np.sin(2 * np.pi * np.cumsum(freq) / SAMPLE_RATE)
    return chirp * np.hanning(n)


def _peak_normalize(signal: np.ndarray, peak: float) -> np.ndarray:
    m = np.max(np.abs(signal))
    return (signal / m * peak) if m > 1e-9 else signal


# ── Per-category generators ────────────────────────────────────────────────────

def gen_wind(rng: np.random.Generator) -> np.ndarray:
    base = _band_limit(_seamless_noise(N, 1.6, rng), 40, 1800)
    gust = _periodic_envelope(N, cycles=3, rng=rng, base=0.5, depth=0.5)
    flutter = _periodic_envelope(N, cycles=7, rng=rng, base=1.0, depth=0.15)
    return _peak_normalize(base * gust * flutter, 0.5)


def gen_forest(rng: np.random.Generator) -> np.ndarray:
    breeze = _band_limit(_seamless_noise(N, 1.2, rng), 400, 7000)
    breeze = breeze * _periodic_envelope(N, cycles=4, rng=rng, base=0.5, depth=0.4)

    n_chirps  = 6
    impulses  = np.zeros(N)
    for pos in rng.integers(0, N, size=n_chirps):
        impulses[pos] += rng.uniform(0.6, 1.0)
    kernel = np.zeros(N)
    chirp_kernel = _chirp_kernel(rng)
    kernel[:len(chirp_kernel)] = chirp_kernel
    chirps = _peak_normalize(
        np.fft.irfft(np.fft.rfft(impulses) * np.fft.rfft(kernel), n=N), 1.0
    )
    return _peak_normalize(breeze * 0.6 + chirps * 0.35, 0.4)


def gen_crowd(rng: np.random.Generator) -> np.ndarray:
    murmur = _formant_noise(N, rng, centers_hz=[300, 600, 1200, 2000], bandwidth_hz=250)
    slow   = _periodic_envelope(N, cycles=11, rng=rng, base=0.6, depth=0.25)
    faster = _periodic_envelope(N, cycles=17, rng=rng, base=1.0, depth=0.2)
    return _peak_normalize(murmur * slow * faster, 0.4)


GENERATORS = {
    "wind":   gen_wind,
    "forest": gen_forest,
    "crowd":  gen_crowd,
}


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, generator in GENERATORS.items():
        rng = np.random.default_rng(seed=_SEEDS[name])
        audio = generator(rng).astype(np.float32)
        out_path = OUT_DIR / f"{name}.wav"
        sf.write(str(out_path), audio, SAMPLE_RATE, subtype="PCM_16")
        print(f"wrote {out_path.relative_to(OUT_DIR.parent.parent)}  "
              f"({len(audio) / SAMPLE_RATE:.1f}s, peak={np.max(np.abs(audio)):.3f})")


if __name__ == "__main__":
    main()
