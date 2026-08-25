# Ambient background sounds

ScrollTone's multi-voice narration can optionally mix a quiet ambient bed
(rain, wind, ocean, fire, forest, crowd) under a chapter's narration when the
text clearly implies that setting.

## Where these come from

**They are not third-party recordings.** ScrollTone is fully local and
Dockerized, and pulling ambient clips from a live sound API (e.g. Freesound)
at generation time would break that guarantee. Bundling real third-party
recordings was considered, but that means tracking a license per clip
indefinitely — a maintenance and compliance burden for six looping beds.

Instead, every loop in `backend/assets/ambience/` is **procedurally
synthesized from scratch** by `scripts/generate_ambience.py`, using plain
DSP (shaped noise + periodic amplitude modulation, built directly in the
frequency domain — see that script's docstring for how). There is no
third-party asset here and nothing to license: the six `.wav` files are
100% original, generated output, committed to the repo like any other
generated artifact.

| File | Category | Duration | Technique |
|---|---|---|---|
| `rain.wav`   | rain   | 24s | pink-noise hiss + circular droplet impulses |
| `wind.wav`   | wind   | 24s | brown noise, low-band, slow gust envelope |
| `ocean.wav`  | ocean  | 24s | low rumble + mid-band foam, wave-swell envelope |
| `fire.wav`   | fire   | 24s | mid-band hiss + circular crackle impulses |
| `forest.wav` | forest | 24s | broadband breeze + occasional bird-chirp bursts |
| `crowd.wav`  | crowd  | 24s | formant-shaped murmur noise, babble-rate envelope |

Each file loops with no audible seam: it's generated as one exact period of
a periodic signal (via inverse FFT into a fixed-length buffer), so tiling it
back-to-back for however long a scene runs produces zero discontinuity —
verified by checking sample[0] vs sample[-1] against typical adjacent-sample
deltas after generation.

## Regenerating

```
python scripts/generate_ambience.py
```

This overwrites `backend/assets/ambience/*.wav`. Re-run it if you want
different-sounding beds (each category has a fixed RNG seed for
reproducibility — change `_SEEDS` in the script to get variation) or want to
add a new category (also update `AMBIENCE_CATEGORIES` in
`backend/ambience.py` so the scene-tagging LLM can actually choose it).

## Why not fewer/more categories

The six categories match what `backend/ambience.py` asks the local LLM to
tag chapters with. The LLM is restricted to choosing only from this list —
never allowed to invent a category with no backing audio file.
