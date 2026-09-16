"""
Audio file utilities.

Responsibilities:
- Writing numpy arrays to WAV files
- Applying broadcast-style audio enhancement via ffmpeg (optional)
- Converting WAV to MP3 with embedded ID3 metadata and cover art
"""


def enhance_wav(path: str) -> None:
    """Apply broadcast-style audio enhancement to a WAV file (in-place).

    Pipeline:
      1. Compression  — evens out loud/quiet (threshold=-18dB, ratio=3:1, attack=5ms, release=50ms)
      2. EQ +2dB @ 200 Hz — adds warmth/depth to the voice
      3. EQ -1dB @ 8 kHz  — reduces harshness/sibilance

    Requires ffmpeg to be available on PATH.
    If ffmpeg fails the original file is left untouched.
    """
    import os
    import subprocess

    tmp = path + ".enhanced.wav"
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", path,
                "-af",
                "acompressor=threshold=-18dB:ratio=3:attack=5:release=50,"
                "equalizer=f=200:width_type=o:width=2:g=2,"
                "equalizer=f=8000:width_type=o:width=2:g=-1",
                tmp,
            ],
            check=True,
            capture_output=True,
        )
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


def change_tempo(path: str, tempo: float) -> None:
    """Time-stretch a WAV file in-place without changing pitch, via ffmpeg's
    atempo filter (e.g. tempo=0.85 → 15% slower, same tone/pitch).

    Chatterbox has no native speaking-rate control, so this is the only way
    to slow its output down while keeping the cloned voice's tone intact.
    ffmpeg's atempo filter only accepts 0.5-2.0 per instance — plenty for our
    UI-exposed range — so no filter chaining is needed.

    Requires ffmpeg to be available on PATH.
    If ffmpeg fails the original file is left untouched.
    """
    import os
    import subprocess

    tempo = max(0.5, min(tempo, 2.0))
    if abs(tempo - 1.0) < 1e-3:
        return

    tmp = path + ".tempo.wav"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-af", f"atempo={tempo}", tmp],
            check=True,
            capture_output=True,
        )
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


def generate_breath(sample_rate: int = 24000, duration: float = 0.35,
                     intensity: float = 0.035, rng=None):
    """Synthesize a short breath-like noise swell for natural narration pauses.

    Chatterbox has no native breath/pause modeling — chunks come back from it
    and get stitched with dead silence, which reads as robotic over a full
    chapter. This generates a soft, band-limited noise burst (quick rise,
    slower fall — like an inhale) to splice between chunks instead of silence.
    Pure numpy (no scipy dep): white noise through a one-pole lowpass strips
    the harsh hiss down to something breathy, then an asymmetric envelope
    shapes it.
    """
    import numpy as np

    if rng is None:
        rng = np.random.default_rng()

    n = max(1, int(sample_rate * duration))
    noise = rng.standard_normal(n).astype(np.float32)

    alpha = 0.06  # one-pole lowpass: y[i] = a*x[i] + (1-a)*y[i-1]
    filtered = np.empty(n, dtype=np.float32)
    prev = 0.0
    for i in range(n):
        prev = alpha * noise[i] + (1 - alpha) * prev
        filtered[i] = prev

    attack  = max(1, int(n * 0.3))
    release = n - attack
    envelope = np.empty(n, dtype=np.float32)
    envelope[:attack]  = np.linspace(0.0, 1.0, attack, dtype=np.float32)
    envelope[attack:]  = np.linspace(1.0, 0.0, release, dtype=np.float32)

    breath = filtered * envelope
    peak = float(np.max(np.abs(breath))) or 1.0
    return (breath / peak * intensity).astype(np.float32)


def analyze_reference_quality(path: str) -> dict:
    """Cheap, dependency-light heuristics on a voice-clone reference clip.

    Not a substitute for listening to it — just flags the two most common,
    fixable causes of "the clone still sounds flat/robotic": a reference clip
    too short for the model to lock onto real prosody, and a reference clip
    that's itself flat/monotone (low energy variation), which the clone tends
    to inherit wholesale. Uses only numpy/soundfile — no new dependency.

    Returns {"duration": float, "warnings": [str, ...]}.
    """
    import numpy as np
    import soundfile as sf

    info = sf.info(path)
    duration = info.duration

    audio, sr = sf.read(path, dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)

    warnings = []
    if duration < 8.0:
        warnings.append(
            f"Reference clip is only {duration:.1f}s — cloning tends to come out flatter/more "
            "robotic under ~8s of reference. 10-20s of natural, expressive speech works best."
        )

    frame = max(1, int(sr * 0.03))
    hop = max(1, frame // 2)
    n_frames = max(0, (len(audio) - frame) // hop + 1)
    if n_frames > 4:
        rms = np.array([
            np.sqrt(np.mean(audio[i * hop: i * hop + frame] ** 2))
            for i in range(n_frames)
        ], dtype=np.float32)
        peak = float(rms.max()) if len(rms) else 0.0
        voiced = rms[rms > peak * 0.1] if peak > 0 else rms
        if len(voiced) > 4 and float(voiced.mean()) > 0:
            coeff_var = float(voiced.std() / voiced.mean())
            if coeff_var < 0.25:
                warnings.append(
                    "Reference clip sounds fairly flat/monotone (low energy variation) — "
                    "the clone tends to inherit that flatness. A more animated, "
                    "naturally-inflected reading clones with noticeably more expression."
                )

    return {"duration": duration, "warnings": warnings}


def write_wav(path: str, audio_array, sample_rate: int = 24000) -> None:
    """Write a numpy float32 audio array to a WAV file."""
    import soundfile as sf
    sf.write(path, audio_array, sample_rate)


def to_mp3(wav_path: str, mp3_path: str, bitrate: int, *,
           title: str = "", album: str = "", artist: str = "",
           track: int = 0, cover_data: bytes = None,
           cover_mime: str = "image/jpeg") -> None:
    """Convert WAV → MP3 and embed ID3 metadata + cover art."""
    from pydub import AudioSegment
    from mutagen.id3 import (ID3, TIT2, TPE1, TALB, TRCK, APIC, TCON,
                              ID3NoHeaderError)

    seg = AudioSegment.from_wav(wav_path)
    seg.export(mp3_path, format="mp3", bitrate=f"{bitrate}k")

    try:
        tags = ID3(mp3_path)
    except ID3NoHeaderError:
        tags = ID3()

    if title:     tags["TIT2"] = TIT2(encoding=3, text=title)
    if artist:    tags["TPE1"] = TPE1(encoding=3, text=artist)
    if album:     tags["TALB"] = TALB(encoding=3, text=album)
    if track > 0: tags["TRCK"] = TRCK(encoding=3, text=str(track))
    tags["TCON"] = TCON(encoding=3, text="Audiobook")
    if cover_data:
        tags["APIC"] = APIC(encoding=3, mime=cover_mime, type=3,
                            desc="Cover", data=cover_data)
    tags.save(mp3_path)
