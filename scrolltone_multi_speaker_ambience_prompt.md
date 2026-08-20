The prompt

You are working on ScrollTone, a fully local, Dockerized TTS audiobook generator (Kokoro for synthesis, FastAPI backend, local LLM for multi-character voice assignment). You are improving two things: making multi-character narration sound natural and consistent, and adding immersive ambient background sound (e.g. rain, wind, crowd noise) that matches what's happening in the text.

Work through this end to end without stopping to ask me questions you can reasonably resolve yourself from the codebase or by making a documented, reversible decision. Only interrupt me for something that genuinely needs my judgment (see "Stop and ask me" at the end). Otherwise, keep going, verify your own work, and only report back once you've actually generated and listened-to (via ffprobe/waveform inspection, since you can't literally listen) a test sample that demonstrates it working.

Phase 0 — Understand what exists
Read through the current character-voice assignment pipeline: how it detects speakers, how it maps them to Kokoro voices, and whether that mapping currently persists across an entire book or only within a chunk/ chapter (this is probably the core bug behind "unnatural" multi-character output — check for it explicitly).
Read through the audio generation/concatenation pipeline: how chapter audio gets built and stitched, what format it's in, and where a mixing step (dialogue + ambience) would naturally slot in.
Summarize what you find in 5-10 lines before proceeding, so the rest of your work is grounded in the real architecture, not assumptions.
Phase 1 — Consistent character voices across the whole book
Build (or fix) a persistent character → voice registry that's scoped to the whole book, not per-chunk or per-chapter. A character should sound the same in chapter 1 and chapter 20.
Handle re-identification: characters referred to by name, pronoun, title, or nickname should resolve to the same registry entry. Use the existing local LLM for this if it's already doing dialogue attribution — extend its prompt/schema rather than bolting on a second system if you can avoid it.
Handle the narrator distinctly from named characters, and handle ambiguous/unattributed dialogue with a sane fallback (don't crash, don't silently mis-assign to the wrong recurring character).
Persist the registry (e.g. alongside the book's existing metadata/cache) so re-running generation for later chapters doesn't reassign voices.
Phase 2 — Detect scene/ambience cues from the text
Using the local LLM, tag each chapter or scene with ambient context where the text clearly implies one: weather (rain, storm, wind), setting (forest, tavern, battlefield, ocean, city street), and notable ambient events. Be conservative — only tag when the text actually supports it, not for every scene.
Output this as structured data (e.g. a list of {start_offset, cue, confidence} per chapter) so it can drive audio mixing downstream and be inspected/debugged.
Phase 3 — Source the ambient sound (flag the tension with "fully local")
ScrollTone's whole premise is fully local and Dockerized. Pulling live from an online sound API (e.g. Freesound) at generation time breaks that. Default to bundling a small curated set of CC0/public-domain ambient loops (rain, wind, crowd, fire, ocean, forest — whatever covers the common cues from Phase 2) into the repo/Docker image at build time, sourced once, licensed clearly, not fetched per-run.
If you do this, add a THIRD_PARTY_SOUNDS.md (or similar) listing each clip's source and license so this doesn't turn into an unlicensed-asset problem later.
If bundling isn't feasible for some reason you discover in the codebase, don't silently fall back to a live API — that's a real product decision, not yours to make silently. Note it under "Stop and ask me" instead and proceed with everything else.
Phase 4 — Mix ambience under the narration
Implement ducking: ambient bed audio should sit clearly under dialogue/ narration in volume, not compete with it. Loop/crossfade the ambient clip seamlessly for the duration of the scene rather than looping with audible seams.
Normalize levels so ambience doesn't clip or overpower speech, and so loudness is consistent across chapters.
Keep this mixing step optional/toggleable — some listeners will want narration-only. Don't hardcode ambience as always-on.
Phase 5 — Test it for real
Generate a real test sample: pick (or synthesize) a short passage that has at least two distinct characters and one clear ambient cue (e.g. a rain scene with dialogue).
Run it through the full updated pipeline and produce an actual audio file.
Verify programmatically: confirm the output file is valid audio, check duration and channel/level stats with ffprobe, confirm no clipping, and confirm (by checking your own registry/logs, not just assuming) that the same character's voice ID was used consistently if they appear more than once in the sample.
If anything fails, fix it and re-run this phase — don't report success until a generated sample actually passes these checks.
Phase 6 — Report back

When everything above is done and verified, report:

What changed, file by file, briefly.
Where the test sample audio file is.
The ffprobe/level-check results you used to confirm it worked.
Any assumptions or tradeoffs you made along the way.
Anything flagged under "Stop and ask me" below.
Stop and ask me

Only interrupt before finishing if you hit one of these:

The "fully local vs. live sound API" tradeoff from Phase 3 can't be resolved by bundling (e.g. licensing genuinely blocks it).
The existing character-voice pipeline is architected in a way where "make it book-scoped instead of chunk-scoped" would require a breaking change to how books are processed/cached — flag the scope of that before doing it.
Anything that would meaningfully increase Docker image size or add a new runtime dependency that conflicts with the "fully local, lightweight" design goal.

Everything else: use your judgment, document the decision inline in a commit message or code comment, and keep moving.