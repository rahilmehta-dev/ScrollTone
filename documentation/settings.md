# All settings

| Setting | Default | Description |
|---------|---------|-------------|
| TTS Engine | Kokoro | Kokoro (built-in voices) or Chatterbox (clone a voice from an uploaded clip) — see [engines.md](engines.md). Higgs Audio V2 support still exists server-side but isn't offered here in this build |
| Narrator Voice | `af_heart` | Voice used for narration (and all speech in single-voice mode). Kokoro only. |
| Speed | `1.0×` | Playback speed (0.5 – 2.5) |
| Output Format | WAV | WAV or MP3 (MP3 embeds cover art & metadata) |
| MP3 Bitrate | 192 kbps | 128 / 192 / 256 / 320 kbps |
| Merge Chapters | On | Produce a single combined file in addition to per-chapter files |
| Device | Auto | Options shown are limited to what this server can actually use (e.g. a Docker container with no GPU passthrough only offers Auto/CPU) — see [installation.md](installation.md#nvidia-gpu-passthrough-linux) for enabling CUDA in Docker |
| Transformer G2P | Off | Better pronunciation, much slower, downloads 457 MB extra on first use |
| Enhance Audio | Off | ffmpeg: compression + 200 Hz warmth + 8 kHz cut. Requires `ffmpeg` on PATH |
| Multi-voice | Off | LLM speaker attribution via Ollama. Requires Ollama running locally. Kokoro only |
| Ambient Sound | Off | LLM scene-cue detection (rain, wind, ocean, fire, forest, crowd) via Ollama, mixed quietly under narration. Loops are procedurally generated, not third-party recordings — see [ambience.md](ambience.md) |
| Ollama URL | `http://localhost:11434` | URL of your local Ollama instance (shared by Multi-voice and Ambient Sound) |
| Ollama Model | `phi3:mini` | Model used for speaker attribution / scene-cue detection |
| Max Chunk Size | `500` chars | Max characters per TTS synthesis call |
| Chapter Silence | `1.0` s | Silence gap between chapters in merged file |
| Min Chapter Length | `200` chars | Skip EPUB sections shorter than this |
| Kokoro Parallel Workers | `1` | Concurrent Kokoro subprocesses per chapter, each with its own model instance (RAM scales ~linearly, ~1.5GB/worker). Leave at 1 for the faster in-process path with no subprocess/model-load overhead — only worth raising on machines with CPU cores to spare |
| Chatterbox Speed | `1.0×` | Post-hoc ffmpeg time-stretch — Chatterbox has no native rate control |
| Chatterbox Parallel Workers | `1` | Concurrent Chatterbox subprocesses per chapter (RAM scales ~linearly per worker) |
| Chatterbox CFG Weight | `0.3` | Lower = more expressive, less tied to the reference clip's exact delivery |
| Chatterbox Exaggeration | `0.7` | Emotional intensity of the delivery (~0.5 is neutral) |
| Chatterbox Temperature | `0.8` | Sampling randomness — higher adds natural sentence-to-sentence variation |
| Chatterbox Breathing Pauses | On | Splices a short synthesized breath between chunk boundaries instead of dead silence |

Higgs Audio V2's settings (Temperature, Top P, Top K, Parallel Workers) aren't listed here since Higgs isn't currently selectable in the UI — see the note in [engines.md](engines.md#optional-higgs-audio-v2--chatterbox-engines) if calling it directly via the API.
