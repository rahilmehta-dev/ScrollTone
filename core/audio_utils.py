"""
Audio file utilities.

Responsibilities:
- Writing numpy arrays to WAV files
- Converting WAV to MP3 with embedded ID3 metadata and cover art
"""


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
