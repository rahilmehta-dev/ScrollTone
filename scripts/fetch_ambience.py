"""
Fetches the real-recording ambient beds (rain, ocean, fire, clock) into
backend/assets/ambience/.

These four categories used to be procedurally synthesized like the rest
(see scripts/generate_ambience.py) but synthesized noise-and-modulation
beds sound harsh and obviously synthetic sitting under narration. Real
recordings read better -- so these four are instead pulled once from
tgstation/tgstation, a long-running open-source game whose sound assets are
licensed CC BY-SA 3.0 (see backend/assets/ambience/ATTRIBUTION.md and that
repo's README `## LICENSE` section). "Once" is the operative word: this is
a build-time step you re-run deliberately, not a live API call ScrollTone
makes during audio generation -- the fully-local/no-live-dependency
guarantee for actual narration is unaffected.

wind/forest/crowd remain synthesized in scripts/generate_ambience.py: no
similarly well-licensed, similarly good-sounding real recording has been
found for those yet. See documentation/ambience.md for that gap list.

Requires ffmpeg on PATH (already a project dependency -- see
requirements.txt -- used elsewhere for tempo-shifting and enhancement).

Run once (output is committed to the repo, not regenerated at runtime):
    python scripts/fetch_ambience.py
"""
import subprocess
import urllib.request
from pathlib import Path

OUT_DIR = Path(__file__).parent.parent / "backend" / "assets" / "ambience"
SAMPLE_RATE = 24000   # matches Kokoro's output rate -- see generate_ambience.py

_BASE = "https://raw.githubusercontent.com/tgstation/tgstation/master/sound"

# (output category name, source path within tgstation's sound/ tree)
SOURCES = {
    "rain":  "ambience/weather/rain/rain_mid.ogg",
    "ocean": "ambience/beach/shore.ogg",
    "fire":  "effects/roaring_fire.ogg",
    "clock": "ambience/misc/ticking_clock.ogg",
}


def _fetch_and_convert(category: str, source_path: str, tmp_dir: Path) -> None:
    ogg_path = tmp_dir / f"{category}.ogg"
    wav_path = OUT_DIR / f"{category}.wav"

    urllib.request.urlretrieve(f"{_BASE}/{source_path}", ogg_path)

    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(ogg_path),
         "-ac", "1", "-ar", str(SAMPLE_RATE), "-sample_fmt", "s16", str(wav_path)],
        check=True,
    )
    print(f"wrote {wav_path.relative_to(OUT_DIR.parent.parent.parent)}  (from {source_path})")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for category, source_path in SOURCES.items():
            _fetch_and_convert(category, source_path, tmp_dir)


if __name__ == "__main__":
    main()
