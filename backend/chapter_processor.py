"""Per-chapter synthesis: title announcement, narration (single- or
multi-voice), ambience mixing, and file writing.

Extracted from pipeline.py's convert_book(), where this used to be a
~220-line nested closure — as a class, what state a chapter run reads and
returns is an explicit constructor/method contract instead of implicit
closure scope. One ChapterProcessor is built per job and its `process()`
is called once per chapter, in order; `done_count` is the only state it
accumulates itself; `voice_mapper` / `ambience_log` are shared, mutable
objects owned by the caller (convert_book) and mutated in place here.
"""
import json
import os
import re

import numpy as np

from backend.audio import write_wav, to_mp3, enhance_wav, change_tempo
from backend.attribution import attribute_speakers
from backend.ambience import detect_ambience_cues
from backend.mixing import build_ambience_track, mix_ambience_under_narration, normalize_loudness
from backend.engines.runner import synthesize_chapter, EngineNotInstalled
from backend.chunking import split_sentences_into_chunks, interleave_breaths


class ChapterProcessor:
    def __init__(self, *, job_state, settings, emitter, engine, pipeline,
                 voice_mapper, registry_path, ambience_enabled, ambience_log,
                 ambience_log_path, book_stem, total_chapters, breath_rng,
                 sample_rate, announce_title=True):
        self.job_state = job_state
        self.settings = settings
        self.emitter = emitter
        self.engine = engine
        self.pipeline = pipeline
        self.voice_mapper = voice_mapper
        self.registry_path = registry_path
        self.ambience_enabled = ambience_enabled
        self.ambience_log = ambience_log
        self.ambience_log_path = ambience_log_path
        self.book_stem = book_stem
        self.total_chapters = total_chapters
        self.breath_rng = breath_rng
        self.sample_rate = sample_rate
        # The Preview & Tweak job (backend/pipeline.py run_preview_job) reuses
        # this class for a synthetic single "chapter" — the sample text — and
        # doesn't want "Preview Sample" spoken as a title prefix the way a
        # real chapter's title is.
        self.announce_title = announce_title
        self.done_count = 0

    def _narrate(self, texts: list[str], chapter_number: int, progress_cb=None) -> list:
        """Synthesize `texts` with the chapter's active engine.

        Kokoro normally runs in-process via the already-loaded `self.pipeline`
        — fastest path, no subprocess/model-load overhead, used whenever
        Kokoro Parallel Workers is left at 1 (the default). Set above 1 and
        it instead fans out across that many subprocess workers (each its
        own KPipeline instance) via runner.synthesize_chapter, the same
        mechanism Chatterbox's parallel workers already use — a single
        in-process pipeline can't be handed to multiple threads safely, so
        real parallelism means separate processes.

        Higgs/Chatterbox always run out-of-process via
        runner.synthesize_chapter — any per-chunk failures or a safety abort
        are logged but don't raise, matching the Kokoro path's per-chunk
        skip-and-continue.
        """
        settings, engine, log = self.settings, self.engine, self.emitter.log

        kokoro_workers = settings.get("kokoro_workers", 1)
        if engine == "kokoro" and (kokoro_workers <= 1 or len(texts) <= 1):
            out = []
            for chunk_index, chunk_text in enumerate(texts):
                if self.job_state["stop_event"].is_set():
                    raise StopIteration
                try:
                    for _, _, audio in self.pipeline(chunk_text, voice=settings["voice"], speed=settings["speed"]):
                        out.append(audio)
                except StopIteration:
                    raise
                except Exception as error:
                    log(f"   ! Ch{chapter_number} chunk {chunk_index + 1} skipped: {error}")
                if progress_cb:
                    progress_cb(chunk_index + 1, len(texts))
            return out

        extra_config = None
        if engine == "chatterbox":
            extra_config = {
                "cfg_weight":   settings.get("chatterbox_cfg_weight", 0.3),
                "exaggeration": settings.get("chatterbox_exaggeration", 0.7),
                "temperature":  settings.get("chatterbox_temperature", 0.8),
            }
        elif engine == "higgs":
            extra_config = {
                "temperature": settings.get("higgs_temperature", 0.3),
                "top_p":       settings.get("higgs_top_p", 0.95),
                "top_k":       settings.get("higgs_top_k", 50),
            }
        elif engine == "kokoro":
            extra_config = {
                "voice": settings["voice"], "speed": settings["speed"],
                "lang_code": settings["lang_code"],
            }
        if engine == "chatterbox":
            num_workers = settings.get("chatterbox_workers", 1)
        elif engine == "kokoro":
            num_workers = kokoro_workers
        else:
            num_workers = 1
        try:
            audio_arrays, engine_result = synthesize_chapter(
                engine, texts, settings.get("reference_wav"), settings["device"],
                on_progress=(lambda i, n: progress_cb(i, n)) if progress_cb else (lambda i, n: None),
                stop_check=self.job_state["stop_event"].is_set,
                extra_config=extra_config,
                num_workers=num_workers,
            )
        except EngineNotInstalled as error:
            log(f"   ! {error}")
            return []
        if engine_result.get("stopped_by_user"):
            log(f"   [{engine}] Stopped by user mid-chapter.")
        for err in engine_result.get("errors", []):
            log(f"   ! [{engine}] {err}")
        return audio_arrays

    def process(self, chapter_index: int, title: str, text: str):
        job_state, settings, emitter = self.job_state, self.settings, self.emitter
        log, push, prog = emitter.log, emitter.push, emitter.progress
        sample_rate = self.sample_rate

        if job_state["stop_event"].is_set():
            raise StopIteration

        chapter_number = chapter_index + 1
        log(f"── Chapter {chapter_number}/{self.total_chapters}: {title}")
        log(f"   {len(text):,} chars")

        chapter_audio = []

        # ── Chapter title announcement ────────────────────────────────
        # Prepend: 0.5 s silence → spoken title → 0.75 s silence
        if self.announce_title:
            try:
                title_frames = self._narrate([title], chapter_number)
                if title_frames:
                    chapter_audio.append(np.zeros(int(sample_rate * 0.5), dtype=np.float32))
                    chapter_audio.extend(title_frames)
                    chapter_audio.append(np.zeros(int(sample_rate * 0.75), dtype=np.float32))
            except StopIteration:
                raise
            except Exception as title_error:
                log(f"   ! Title announcement skipped: {title_error}")

        if self.voice_mapper:
            # ── Multi-voice path ──────────────────────────────────────
            # One LLM call per chapter to attribute all dialogue
            log(f"   [Ollama] → {settings['ollama_model']}  "
                f"({len(text):,} chars, {settings['ollama_url']})")
            try:
                segments = attribute_speakers(
                    text, settings["ollama_url"], settings["ollama_model"],
                    known_characters=self.voice_mapper.known_names(),
                )
                dialogue = [segment for segment in segments if segment["type"] == "dialogue"]
                narration = [segment for segment in segments if segment["type"] == "narration"]
                log(f"   [Ollama] ← {len(segments)} segments  "
                    f"({len(dialogue)} dialogue, {len(narration)} narration)")
                # Log each new character assignment
                for segment in dialogue:
                    speaker = segment.get("speaker")
                    gender  = segment.get("gender") or "?"
                    if speaker:
                        voice = self.voice_mapper.get_voice(speaker, segment.get("gender"))
                        preview = segment["text"][:60].replace("\n", " ")
                        log(f"   [Ollama]   {speaker} ({gender}) → {voice}  \"{preview}…\"")
                log(f"   Characters so far: {self.voice_mapper.summary()}")
                self.voice_mapper.save(self.registry_path)
            except Exception as error:
                log(f"   [Ollama] ! Attribution failed: {error}")
                log(f"   [Ollama] ! Falling back to single voice ({settings['voice']})")
                segments = [{"type": "narration", "text": text,
                             "speaker": None, "gender": None}]

            # Flatten segments → sub-chunks with per-chunk voice
            voice_chunks = []
            for segment in segments:
                segment_text = segment.get("text", "").strip()
                if not segment_text:
                    continue
                voice = self.voice_mapper.get_voice(segment.get("speaker"), segment.get("gender"))
                for sub_chunk in split_sentences_into_chunks(segment_text, settings["chunk_size"]):
                    voice_chunks.append((voice, sub_chunk))

            total_chunks      = len(voice_chunks)
            progress_interval = max(1, total_chunks // 20)
            push({"type": "ch_start", "ch_i": chapter_index, "chunks": total_chunks})

            for chunk_index, (voice, chunk) in enumerate(voice_chunks):
                if job_state["stop_event"].is_set():
                    raise StopIteration
                try:
                    for _, _, audio in self.pipeline(chunk, voice=voice, speed=settings["speed"]):
                        chapter_audio.append(audio)
                except StopIteration:
                    raise
                except Exception as error:
                    log(f"   ! Ch{chapter_number} chunk {chunk_index + 1} skipped: {error}")
                if (chunk_index + 1) % progress_interval == 0 or chunk_index == total_chunks - 1:
                    push({"type": "ch_prog", "ch_i": chapter_index,
                          "pct": round((chunk_index + 1) / total_chunks, 3)})

        else:
            # ── Single-voice path ───────────────────────────────────
            chunks       = split_sentences_into_chunks(text, settings["chunk_size"])
            total_chunks = len(chunks)
            log(f"   {total_chunks} chunks  (engine={self.engine})")
            push({"type": "ch_start", "ch_i": chapter_index, "chunks": total_chunks})

            # Kokoro is fast enough that many chunks fire per second, so
            # its progress is throttled to ~20 UI updates/chapter (matches
            # prior behavior). Higgs/Chatterbox chunks take seconds-to-
            # minutes each, so every chunk gets its own update.
            progress_interval = max(1, total_chunks // 20) if self.engine == "kokoro" else 1

            def _on_chunk_progress(done_n, total_n):
                if done_n % progress_interval == 0 or done_n == total_n:
                    push({"type": "ch_prog", "ch_i": chapter_index,
                          "pct": round(done_n / total_n, 3)})

            narrated = self._narrate(chunks, chapter_number, progress_cb=_on_chunk_progress)
            if self.engine == "chatterbox" and settings.get("chatterbox_breaths", True) and len(narrated) > 1:
                narrated = interleave_breaths(narrated, sample_rate, self.breath_rng)
            chapter_audio.extend(narrated)

        if not chapter_audio:
            log(f"   (no audio generated)\n")
            push({"type": "ch_skip", "ch_i": chapter_index})
            return (chapter_index, None, None, 0.0)

        combined_audio = np.concatenate(chapter_audio)

        if self.ambience_enabled:
            try:
                cues = detect_ambience_cues(text, settings["ollama_url"], settings["ollama_model"])
                self.ambience_log[str(chapter_index)] = {"title": title, "cues": cues}
                self.ambience_log_path.write_text(json.dumps(self.ambience_log, indent=2))
                if cues:
                    log(f"   [Ambience] " + ", ".join(
                        f"{c['cue']}@{c['confidence']:.2f}" for c in cues))
                    ambience_track = build_ambience_track(
                        cues, len(text), len(combined_audio), sample_rate)
                    if ambience_track is not None:
                        combined_audio = mix_ambience_under_narration(
                            combined_audio, ambience_track, sample_rate)
                        log(f"   [Ambience] mixed under narration ({len(combined_audio)/sample_rate:.1f}s)")
                else:
                    log("   [Ambience] no clear cue for this chapter — narration only")
            except Exception as error:
                log(f"   [Ambience] ! Detection/mixing skipped: {error}")

        combined_audio = normalize_loudness(combined_audio)

        safe_title     = re.sub(r"[^\w\s-]", "", title)[:35].strip()
        wav_filename   = f"{self.book_stem}_{safe_title}.wav"
        wav_path       = os.path.join(settings["out_dir"], wav_filename)
        write_wav(wav_path, combined_audio, sample_rate)

        chatterbox_speed = settings.get("chatterbox_speed", 1.0)
        if self.engine == "chatterbox" and chatterbox_speed != 1.0:
            try:
                change_tempo(wav_path, chatterbox_speed)
            except Exception as error:
                log(f"   ! Speed adjustment skipped (Ch{chapter_number}): {error}")

        if settings.get("enhance"):
            try:
                enhance_wav(wav_path)
            except Exception as error:
                log(f"   ! Enhancement skipped (Ch{chapter_number}): {error}")

        if settings.get("output_format") == "mp3":
            filename = f"{self.book_stem}_{safe_title}.mp3"
            to_mp3(wav_path, os.path.join(settings["out_dir"], filename),
                   settings["bitrate"],
                   title=title,
                   album=settings.get("book_title_meta") or self.book_stem,
                   artist=settings.get("book_author_meta", ""),
                   track=chapter_number,
                   cover_data=settings.get("cover_data"),
                   cover_mime=settings.get("cover_mime", "image/jpeg"))
            os.remove(wav_path)
        else:
            filename = wav_filename

        duration = len(combined_audio) / sample_rate
        log(f"   Saved: {filename}  ({duration:.1f}s)\n")

        self.done_count += 1
        prog(self.done_count / self.total_chapters, f"{self.done_count}/{self.total_chapters} chapters done")

        push({"type": "file", "filename": filename,
              "duration": duration, "chapter": chapter_number, "title": title})
        return (chapter_index, filename, combined_audio, duration)
