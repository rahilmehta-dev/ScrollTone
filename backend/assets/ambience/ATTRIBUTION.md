# Ambience asset attribution

Most beds in this folder are procedurally synthesized (no third-party
asset, nothing to attribute — see `scripts/generate_ambience.py`). The four
below are real recordings and carry a license obligation.

## Real recordings — CC BY-SA 3.0, from tgstation/tgstation

Source: https://github.com/tgstation/tgstation — "All assets including
icons and sound are under a Creative Commons 3.0 BY-SA license unless
otherwise indicated." (repo README, `## LICENSE` section)
License text: https://creativecommons.org/licenses/by-sa/3.0/

| File | Source path (in tgstation/tgstation) |
|---|---|
| `rain.wav`  | `sound/ambience/weather/rain/rain_mid.ogg` |
| `ocean.wav` | `sound/ambience/beach/shore.ogg` |
| `fire.wav`  | `sound/effects/roaring_fire.ogg` |
| `clock.wav` | `sound/ambience/misc/ticking_clock.ogg` |

Each was downsampled to mono 24kHz PCM16 (`scripts/fetch_ambience.py`) — no
other change to the audio content. CC BY-SA 3.0 requires attribution (this
file) and that these specific files, if redistributed, stay under the same
license — it does not extend to the rest of ScrollTone's code.

## Still synthesized

`wind.wav`, `forest.wav`, `crowd.wav` — see `scripts/generate_ambience.py`.
No equivalently well-licensed real recording has been sourced for these
yet; see `documentation/ambience.md` for the current gap list (plain wind,
forest/woodland, and crowd/tavern/market ambience specifically).
