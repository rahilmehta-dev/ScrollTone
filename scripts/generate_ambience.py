"""
Procedurally synthesizes the bundled ambient background loops.

ScrollTone's premise is fully local and Dockerized. Pulling ambient sound
clips from a live API (e.g. Freesound) at generation time would break that,
and bundling real third-party recordings would mean tracking a license per
clip. Instead, each loop below is synthesized from scratch with plain DSP
(shaped noise + periodic modulation) directly into backend/assets/ambience/
-- there is no third-party asset involved and nothing to license. See
AMBIENCE_SOUNDS.md for the full rationale.

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

_SEEDS = {"rain": 1, "wind": 2, "ocean": 3, "fire": 4, "forest": 5, "crowd": 6}


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


def _circular_impulse_texture(
    n: int, rng: np.random.Generator, rate_per_sec: float, decay_sec: float, amp_range: tuple[float, float]
) -> np.ndarray:
    """Sparse random impulses circularly convolved with an exponential-decay kernel.

    Circular convolution (FFT multiply) of two periodic-N signals stays
    periodic-N, so this stays seamlessly loopable even though the impulse
    positions are random.
    """
    impulses = np.zeros(n, dtype=np.float64)
    count = int(rate_per_sec * n / SAMPLE_RATE)
    for pos in rng.integers(0, n, size=count):
        impulses[pos] += rng.uniform(*amp_range)
    decay_len = int(decay_sec * SAMPLE_RATE)
    kernel = np.zeros(n, dtype=np.float64)
    kernel[:decay_len] = np.exp(-np.linspace(0, 6, decay_len))
    out = np.fft.irfft(np.fft.rfft(impulses) * np.fft.rfft(kernel), n=n)
    return _peak_normalize(out, 1.0)


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

def gen_rain(rng: np.random.Generator) -> np.ndarray:
    hiss     = _band_limit(_seamless_noise(N, 0.5, rng), 300, 10000)
    droplets = _band_limit(_circular_impulse_texture(N, rng, 120, 0.015, (0.3, 1.0)), 1000, 11000)
    return _peak_normalize(0.6 * hiss + 0.5 * droplets, 0.5)


def gen_wind(rng: np.random.Generator) -> np.ndarray:
    base = _band_limit(_seamless_noise(N, 1.6, rng), 40, 1800)
    gust = _periodic_envelope(N, cycles=3, rng=rng, base=0.5, depth=0.5)
    flutter = _periodic_envelope(N, cycles=7, rng=rng, base=1.0, depth=0.15)
    return _peak_normalize(base * gust * flutter, 0.5)


def gen_ocean(rng: np.random.Generator) -> np.ndarray:
    rumble   = _band_limit(_seamless_noise(N, 2.0, rng), 20, 400)
    foam     = _band_limit(_seamless_noise(N, 0.3, rng), 800, 9000)
    swell    = _periodic_envelope(N, cycles=5, rng=rng, base=0.4, depth=0.6)
    foam_env = _periodic_envelope(N, cycles=5, rng=rng, base=0.3, depth=0.5)
    mix = rumble * 0.8 + foam * foam_env * 0.6
    return _peak_normalize(mix * (0.5 + 0.5 * swell), 0.5)


def gen_fire(rng: np.random.Generator) -> np.ndarray:
    hiss    = _band_limit(_seamless_noise(N, 1.0, rng), 200, 6000)
    crackle = _band_limit(_circular_impulse_texture(N, rng, 25, 0.008, (0.4, 1.0)), 800, 9000)
    return _peak_normalize(hiss * 0.5 + crackle * 0.7, 0.45)


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
    "rain":   gen_rain,
    "wind":   gen_wind,
    "ocean":  gen_ocean,
    "fire":   gen_fire,
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
