# Ambient background sounds

ScrollTone's multi-voice narration can optionally mix a quiet ambient bed
(rain, wind, ocean, fire, forest, crowd, clock) under a chapter's narration
when the text clearly puts the listener in that scene.

## Where these come from

Every bed used to be **procedurally synthesized from scratch** (plain DSP —
shaped noise + periodic modulation) specifically to avoid ever bundling a
third-party recording: ScrollTone is fully local and Dockerized, and
pulling clips from a live sound API (e.g. Freesound) *during audio
generation* would break that guarantee, while bundling real recordings
means tracking a license per clip indefinitely.

In practice the synthesized beds sounded harsh and obviously synthetic
sitting under narration — noise-and-modulation reads as noise, not as rain.
So four categories (`rain`, `ocean`, `fire`, `clock`) are now **real
recordings**, pulled once, up front, by `scripts/fetch_ambience.py` from
[tgstation/tgstation](https://github.com/tgstation/tgstation) (CC BY-SA
3.0 — see `backend/assets/ambience/ATTRIBUTION.md` for the exact source
path and license terms per file). "Once, up front" is the important
distinction from the live-API case that was originally ruled out: this is
a deliberate build-time step re-run only when someone wants to refresh the
assets, not something ScrollTone calls over the network while narrating a
book. `wind`, `forest`, and `crowd` are still synthesized by
`scripts/generate_ambience.py` — see **Gaps** below for why.

| File | Category | Source | License |
|---|---|---|---|
| `rain.wav`   | rain   | real recording (tgstation) | CC BY-SA 3.0 |
| `ocean.wav`  | ocean  | real recording (tgstation) | CC BY-SA 3.0 |
| `fire.wav`   | fire   | real recording (tgstation) | CC BY-SA 3.0 |
| `clock.wav`  | clock  | real recording (tgstation) | CC BY-SA 3.0 |
| `wind.wav`   | wind   | synthesized (brown noise, low-band, slow gust envelope) | none — original |
| `forest.wav` | forest | synthesized (broadband breeze + bird-chirp bursts) | none — original |
| `crowd.wav`  | crowd  | synthesized (formant-shaped murmur noise) | none — original |

## Not fighting the narration

Two separate problems used to make ambience compete with the voice instead
of sitting behind it, and both are now handled in `backend/mixing.py`:

- **Frequency masking.** A broadband bed (rain/crowd hiss) occupies the
  same ~250 Hz–4 kHz band that carries speech intelligibility. Turning the
  bed's *overall* volume down doesn't fix that — the energy that's still
  there sits right on top of consonants and sibilance. `_carve_vocal_band`
  attenuates the ambience specifically inside that band (a smooth FFT gain
  dip, not touching narration at all), independent of how loud any given
  moment gets.
- **Ducking depth.** On top of the carve, the existing envelope-follower
  ducking (`AMBIENCE_DUCK_DB`) was made deeper, and the baseline bed level
  (`AMBIENCE_BED_GAIN_DB`) quieter, since real recordings read as more
  "present" than the old synthetic noise at the same nominal level.

Two more fixes came along with switching to real recordings, both in
`backend/mixing.py`:
- **Peak normalization on load** (`_load_bed`) — a real recording's peak
  level has nothing to do with any other file's, unlike the synthesized
  beds (which were all peak-normalized by the same generator). Without
  this, categories would be inconsistently loud relative to each other.
- **Loop-seam crossfade** (`_tile_to_length`) — the synthesized beds are
  *exactly* periodic by construction (see `generate_ambience.py`'s
  docstring), so tiling them needed no crossfade. A real recording's first
  and last sample don't generally match, so tiling one without a crossfade
  clicks on every repeat; `_tile_to_length` now blends the loop's head and
  tail across a short window to prevent that (a no-op for the still-exactly-
  periodic synthesized beds).

## Detecting when to use them

`backend/ambience.py` asks a local Ollama model to tag a chapter with cues
from the fixed category list above, each backed by a short verbatim quote
(verified against the actual text via `str.find`, not trusted blindly) so
the model can never invent a cue with no audio to back it and can't fake a
location that doesn't occur in the text. The prompt is scoped to the scene
itself — sustained sensory detail that puts the listener there (the sound,
feel, or setting of it), not a single keyword, a metaphor, or a mood
untethered from any of these physical sounds actually being present.

## Gaps — no well-licensed real recording sourced yet

Beyond the six original weather/setting categories, the intent is for
ambience to reflect *whatever atmosphere a scene calls for*, not just
literal weather words — a wider set than what's bundled today. Specifically
still missing, after checking GitHub for CC0/CC-BY-SA sources:

- **Plain wind** — a calm open-air breeze, as opposed to storm-intensity
  wind (tgstation's is bundled with blizzard/ashstorm sound, not a good
  substitute on its own).
- **Forest/woodland ambience** — birdsong, rustling leaves. Not present in
  tgstation (a space-station game); no other well-licensed GitHub source
  found for this.
- **Crowd/tavern/market chatter** — not present in tgstation either.
- **One-shot event sounds** (e.g. a door creaking open at a specific
  moment, rather than a continuous background bed) — tgstation actually
  has good candidates for this (`sound/effects/creak/creak1-3.ogg`,
  `sound/effects/doorcreaky.ogg`), but the current mixing pipeline only
  supports one continuous looping bed per chapter scene, not discrete cues
  fired at a specific point in the narration. Using these well would need a
  separate one-shot-cue mixing path, not just another loop category — a
  bigger change than swapping an asset, and not done here.

The realistic source for the three missing ambience categories is
individually CC0-tagged Freesound.org clips rather than another GitHub
grab-bag repo — the ones searched (e.g. `lavenderdotpet/CC0-Public-Domain-Sounds`)
turned out to be short game SFX (UI beeps, impacts), not long ambient
loops.

## Regenerating

```
python scripts/generate_ambience.py   # wind, forest, crowd (synthesized)
python scripts/fetch_ambience.py      # rain, ocean, fire, clock (real recordings)
```

`generate_ambience.py` only ever touches the categories in its own
`GENERATORS` dict — it will not overwrite the real recordings. Adding a new
category means updating `AMBIENCE_CATEGORIES` in `backend/ambience.py` (so
the scene-tagging LLM can choose it) and either generator script.
