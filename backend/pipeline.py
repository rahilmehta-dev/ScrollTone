"""
Conversion job execution.

Responsibilities:
- convert_book()     : full EPUB-to-audio pipeline for one book, run in a
                        background thread so it doesn't block the event loop
- process_chapter()  : per-chapter synthesis (nested closure inside convert_book)

Chapters are processed one at a time, in order, on a single Kokoro pipeline.
"""
import asyncio
import json
import os
import re
from pathlib import Path

import backend.state as state
from backend.epub_parser import (
    _find_epub_cover,
    extract_chapters,
    extract_chapters_from_text,
    get_book_metadata,
)
from backend.audio import write_wav, to_mp3, enhance_wav, change_tempo, generate_breath
from backend.voices import VoiceMapper, REGISTRY_FILENAME
from backend.attribution import attribute_speakers
from backend.ambience import detect_ambience_cues
from backend.mixing import build_ambience_track, mix_ambience_under_narration, normalize_loudness
from backend.engines.runner import synthesize_chapter, EngineNotInstalled

SAMPLE_RATE = 24000   # Kokoro output sample rate


def convert_book(job_state: dict, settings: dict, loop: asyncio.AbstractEventLoop) -> None:
    """Run a full conversion job.

    Pushed messages are JSON strings compatible with the SSE stream format:
        {"type": "log"|"status"|"progress"|"file"}

    Sends None to the queue when complete so the SSE generator can close.
    """

    # ── Push helpers ──────────────────────────────────────────────────────────
    def _push(data: dict):
        loop.call_soon_threadsafe(job_state["queue"].put_nowait, json.dumps(data))

    def log(msg: str):            _push({"type": "log",      "msg": msg})
    def status(msg: str):         _push({"type": "status",   "msg": msg})
    def prog(value, label=""):    _push({"type": "progress", "value": value, "label": label})
    def done():                   loop.call_soon_threadsafe(job_state["queue"].put_nowait, None)

    try:
        import psutil
        from kokoro import KPipeline
        from ebooklib import epub
        import numpy as np

        current_process = psutil.Process(os.getpid())

        def memlog(label: str = ""):
            rss_gb    = current_process.memory_info().rss / 1024**3
            mem_stats = psutil.virtual_memory()
            msg       = (f"[MEM] {label}  "
                         f"process={rss_gb:.2f}GB  "
                         f"system={mem_stats.used/1024**3:.2f}/{mem_stats.total/1024**3:.2f}GB  "
                         f"avail={mem_stats.available/1024**3:.2f}GB")
            log(msg)
            print(msg, flush=True)

        memlog("startup")

        engine = settings.get("engine", "kokoro")

        # Kokoro's pipeline is only needed when it's the active engine (or for
        # Multi-voice, which stays Kokoro-only — the /convert route rejects
        # multi_voice=true combined with a non-Kokoro engine, so that combo
        # never reaches here). Higgs/Chatterbox run out-of-process entirely
        # (backend/engines/runner.py), so skip loading Kokoro's model at all
        # when it won't be used — avoids an unnecessary load and, if
        # Transformer G2P is on, an unnecessary extra weights download.
        pipeline = None
        if engine == "kokoro" or settings.get("multi_voice"):
            status("Loading pipeline…")
            log(f"Initializing pipeline  lang={settings['lang_code']}  trf={settings['trf']}")
            pipeline = KPipeline(lang_code=settings["lang_code"],
                                  repo_id="hexgrad/Kokoro-82M",
                                  trf=settings["trf"], device=settings["device"])
            memlog("after pipeline loaded")
            log(f"Model ready  |  voice={settings['voice']}  speed={settings['speed']:.2f}×\n")
        else:
            workers = settings.get("chatterbox_workers", 1) if engine == "chatterbox" else 1
            worker_note = f"  |  {workers} parallel workers" if workers > 1 else ""
            log(f"Engine: {engine}  |  reference={Path(settings['reference_wav']).name}{worker_note}\n")
            for warning in settings.get("reference_warnings", []):
                log(f"   ! [reference] {warning}")

        # Read and parse the source book (.epub or .txt)
        is_txt = Path(settings["source_path"]).suffix.lower() == ".txt"

        if is_txt:
            status("Reading text file…")
            log(f"Reading: {settings['filename']}")
            settings["book_title_meta"]  = ""
            settings["book_author_meta"] = ""
            settings["cover_data"], settings["cover_mime"] = None, "image/jpeg"
            log("No cover image (plain text upload)")

            with open(settings["source_path"], encoding="utf-8", errors="ignore") as text_file:
                raw_text = text_file.read()
            chapters = extract_chapters_from_text(raw_text, settings["min_ch_len"])
        else:
            status("Reading EPUB…")
            log(f"Reading: {settings['filename']}")
            book = epub.read_epub(settings["source_path"])

            metadata = get_book_metadata(book)
            settings["book_title_meta"]  = metadata["title"]
            settings["book_author_meta"] = metadata["author"]
            settings["cover_data"], settings["cover_mime"] = _find_epub_cover(book)
            if settings["cover_data"]:
                log(f"Cover image found ({len(settings['cover_data']) // 1024} KB, {settings['cover_mime']})")
            else:
                log("No cover image found in EPUB")

            chapters = extract_chapters(book, settings["min_ch_len"])

        selected_indices = settings.get("chapter_indices")
        if selected_indices is not None:
            selected_indices_set = set(selected_indices)
            chapters = [chapter for index, chapter in enumerate(chapters) if index in selected_indices_set]

        if not chapters:
            log("No chapters found in the uploaded file.")
            job_state["status"] = "error"
            done(); return

        log(f"Found {len(chapters)} chapters\n")
        # Seed the chapter progress grid in the UI
        _push({"type": "ch_info",
               "chapters": [{"i": index, "title": chapter_title}
                            for index, (chapter_title, _) in enumerate(chapters)]})

        book_stem     = re.sub(r"[^\w\s-]", "", Path(settings["filename"]).stem)[:50]
        silence_array = np.zeros(int(SAMPLE_RATE * settings["silence"]), dtype=np.float32)
        breath_rng    = np.random.default_rng()

        done_count = 0

        # ── Voice mapper (shared across all chapters for consistency, and ─────
        #    persisted to out_dir so a later re-run for more chapters of the
        #    same book reuses the same character → voice assignments) ────────
        voice_mapper = VoiceMapper(settings["voice"]) if settings.get("multi_voice") else None
        registry_path = Path(settings["out_dir"]) / REGISTRY_FILENAME
        if voice_mapper:
            voice_mapper.load(registry_path)
            log(f"Multi-voice enabled  |  narrator={settings['voice']}  "
                f"model={settings['ollama_model']}  url={settings['ollama_url']}")
            if voice_mapper.known_names():
                log(f"Loaded existing character registry: {voice_mapper.summary()}\n")
            else:
                log("")

        ambience_enabled = bool(settings.get("ambience"))
        ambience_log_path = Path(settings["out_dir"]) / "ambience_cues.json"
        ambience_log: dict = {}
        if ambience_enabled:
            log(f"Ambient sound enabled  |  model={settings['ollama_model']}  url={settings['ollama_url']}\n")

        def _split_chunks(text: str) -> list[str]:
            sentences = re.split(r"(?<=[.!?])\s+", text)
            chunks, current_chunk = [], ""
            for sentence in sentences:
                if len(current_chunk) + len(sentence) + 1 <= settings["chunk_size"]:
                    current_chunk = (current_chunk + " " + sentence).strip()
                else:
                    if current_chunk: chunks.append(current_chunk)
                    current_chunk = sentence
            if current_chunk: chunks.append(current_chunk)
            return chunks

        def _interleave_breaths(audio_chunks: list) -> list:
            """Splice a short breath (or, sometimes, just a plain gap) between
            each chunk boundary — Chatterbox chunks otherwise get concatenated
            back-to-back with zero gap, which reads as rushed/robotic over a
            full chapter. Roughly half of boundaries get an audible breath;
            durations/intensities are randomized so it doesn't sound metronomic.
            """
            stitched = [audio_chunks[0]]
            for chunk in audio_chunks[1:]:
                if breath_rng.random() < 0.55:
                    stitched.append(np.zeros(
                        int(SAMPLE_RATE * breath_rng.uniform(0.08, 0.18)), dtype=np.float32))
                    stitched.append(generate_breath(
                        SAMPLE_RATE,
                        duration=breath_rng.uniform(0.25, 0.4),
                        intensity=breath_rng.uniform(0.02, 0.045),
                        rng=breath_rng,
                    ))
                    stitched.append(np.zeros(
                        int(SAMPLE_RATE * breath_rng.uniform(0.05, 0.12)), dtype=np.float32))
                else:
                    stitched.append(np.zeros(
                        int(SAMPLE_RATE * breath_rng.uniform(0.15, 0.3)), dtype=np.float32))
                stitched.append(chunk)
            return stitched

        # ── Per-chapter synthesis ───────────────────────────────────────────────
        def process_chapter(chapter_index, title, text):
            nonlocal done_count
            if job_state["stop_event"].is_set():
                raise StopIteration

            chapter_number = chapter_index + 1
            total_chapters = len(chapters)
            log(f"── Chapter {chapter_number}/{total_chapters}: {title}")
            log(f"   {len(text):,} chars")

            chapter_audio = []

            def _narrate(texts: list[str], progress_cb=None) -> list:
                """Synthesize `texts` with the chapter's active engine.

                Kokoro runs in-process via the already-loaded `pipeline`.
                Higgs/Chatterbox run out-of-process via runner.synthesize_chapter
                — any per-chunk failures or a safety abort are logged but don't
                raise, matching the Kokoro path's per-chunk skip-and-continue.
                """
                if engine == "kokoro":
                    out = []
                    for chunk_index, chunk_text in enumerate(texts):
                        if job_state["stop_event"].is_set():
                            raise StopIteration
                        try:
                            for _, _, audio in pipeline(chunk_text, voice=settings["voice"], speed=settings["speed"]):
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
                try:
                    audio_arrays, engine_result = synthesize_chapter(
                        engine, texts, settings["reference_wav"], settings["device"],
                        on_progress=(lambda i, n: progress_cb(i, n)) if progress_cb else (lambda i, n: None),
                        stop_check=job_state["stop_event"].is_set,
                        extra_config=extra_config,
                        num_workers=settings.get("chatterbox_workers", 1) if engine == "chatterbox" else 1,
                    )
                except EngineNotInstalled as error:
                    log(f"   ! {error}")
                    return []
                if engine_result.get("stopped_by_user"):
                    log(f"   [{engine}] Stopped by user mid-chapter.")
                for err in engine_result.get("errors", []):
                    log(f"   ! [{engine}] {err}")
                return audio_arrays

            # ── Chapter title announcement ────────────────────────────────
            # Prepend: 0.5 s silence → spoken title → 0.75 s silence
            try:
                title_frames = _narrate([title])
                if title_frames:
                    chapter_audio.append(np.zeros(int(SAMPLE_RATE * 0.5), dtype=np.float32))
                    chapter_audio.extend(title_frames)
                    chapter_audio.append(np.zeros(int(SAMPLE_RATE * 0.75), dtype=np.float32))
            except StopIteration:
                raise
            except Exception as title_error:
                log(f"   ! Title announcement skipped: {title_error}")

            if voice_mapper:
                # ── Multi-voice path ──────────────────────────────────────
                # One LLM call per chapter to attribute all dialogue
                log(f"   [Ollama] → {settings['ollama_model']}  "
                    f"({len(text):,} chars, {settings['ollama_url']})")
                try:
                    segments = attribute_speakers(
                        text, settings["ollama_url"], settings["ollama_model"],
                        known_characters=voice_mapper.known_names(),
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
                            voice = voice_mapper.get_voice(speaker, segment.get("gender"))
                            preview = segment["text"][:60].replace("\n", " ")
                            log(f"   [Ollama]   {speaker} ({gender}) → {voice}  \"{preview}…\"")
                    log(f"   Characters so far: {voice_mapper.summary()}")
                    voice_mapper.save(registry_path)
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
                    voice = voice_mapper.get_voice(segment.get("speaker"), segment.get("gender"))
                    for sub_chunk in _split_chunks(segment_text):
                        voice_chunks.append((voice, sub_chunk))

                total_chunks      = len(voice_chunks)
                progress_interval = max(1, total_chunks // 20)
                _push({"type": "ch_start", "ch_i": chapter_index, "chunks": total_chunks})

                for chunk_index, (voice, chunk) in enumerate(voice_chunks):
                    if job_state["stop_event"].is_set():
                        raise StopIteration
                    try:
                        for _, _, audio in pipeline(chunk, voice=voice, speed=settings["speed"]):
                            chapter_audio.append(audio)
                    except StopIteration:
                        raise
                    except Exception as error:
                        log(f"   ! Ch{chapter_number} chunk {chunk_index + 1} skipped: {error}")
                    if (chunk_index + 1) % progress_interval == 0 or chunk_index == total_chunks - 1:
                        _push({"type": "ch_prog", "ch_i": chapter_index,
                               "pct": round((chunk_index + 1) / total_chunks, 3)})

            else:
                # ── Single-voice path ───────────────────────────────────
                chunks       = _split_chunks(text)
                total_chunks = len(chunks)
                log(f"   {total_chunks} chunks  (engine={engine})")
                _push({"type": "ch_start", "ch_i": chapter_index, "chunks": total_chunks})

                # Kokoro is fast enough that many chunks fire per second, so
                # its progress is throttled to ~20 UI updates/chapter (matches
                # prior behavior). Higgs/Chatterbox chunks take seconds-to-
                # minutes each, so every chunk gets its own update.
                progress_interval = max(1, total_chunks // 20) if engine == "kokoro" else 1

                def _on_chunk_progress(done_n, total_n):
                    if done_n % progress_interval == 0 or done_n == total_n:
                        _push({"type": "ch_prog", "ch_i": chapter_index,
                               "pct": round(done_n / total_n, 3)})

                narrated = _narrate(chunks, progress_cb=_on_chunk_progress)
                if engine == "chatterbox" and settings.get("chatterbox_breaths", True) and len(narrated) > 1:
                    narrated = _interleave_breaths(narrated)
                chapter_audio.extend(narrated)

            if not chapter_audio:
                log(f"   (no audio generated)\n")
                _push({"type": "ch_skip", "ch_i": chapter_index})
                return (chapter_index, None, None, 0.0)

            combined_audio = np.concatenate(chapter_audio)

            if ambience_enabled:
                try:
                    cues = detect_ambience_cues(text, settings["ollama_url"], settings["ollama_model"])
                    ambience_log[str(chapter_index)] = {"title": title, "cues": cues}
                    ambience_log_path.write_text(json.dumps(ambience_log, indent=2))
                    if cues:
                        log(f"   [Ambience] " + ", ".join(
                            f"{c['cue']}@{c['confidence']:.2f}" for c in cues))
                        ambience_track = build_ambience_track(
                            cues, len(text), len(combined_audio), SAMPLE_RATE)
                        if ambience_track is not None:
                            combined_audio = mix_ambience_under_narration(
                                combined_audio, ambience_track, SAMPLE_RATE)
                            log(f"   [Ambience] mixed under narration ({len(combined_audio)/SAMPLE_RATE:.1f}s)")
                    else:
                        log("   [Ambience] no clear cue for this chapter — narration only")
                except Exception as error:
                    log(f"   [Ambience] ! Detection/mixing skipped: {error}")

            combined_audio = normalize_loudness(combined_audio)

            safe_title     = re.sub(r"[^\w\s-]", "", title)[:35].strip()
            wav_filename   = f"{book_stem}_{safe_title}.wav"
            wav_path       = os.path.join(settings["out_dir"], wav_filename)
            write_wav(wav_path, combined_audio, SAMPLE_RATE)

            chatterbox_speed = settings.get("chatterbox_speed", 1.0)
            if engine == "chatterbox" and chatterbox_speed != 1.0:
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
                filename = f"{book_stem}_{safe_title}.mp3"
                to_mp3(wav_path, os.path.join(settings["out_dir"], filename),
                       settings["bitrate"],
                       title=title,
                       album=settings.get("book_title_meta") or book_stem,
                       artist=settings.get("book_author_meta", ""),
                       track=chapter_number,
                       cover_data=settings.get("cover_data"),
                       cover_mime=settings.get("cover_mime", "image/jpeg"))
                os.remove(wav_path)
            else:
                filename = wav_filename

            duration = len(combined_audio) / SAMPLE_RATE
            log(f"   Saved: {filename}  ({duration:.1f}s)\n")

            done_count += 1
            prog(done_count / total_chapters, f"{done_count}/{total_chapters} chapters done")

            _push({"type": "file", "filename": filename,
                   "duration": duration, "chapter": chapter_number, "title": title})
            return (chapter_index, filename, combined_audio, duration)

        # ── Execution — one chapter at a time, in order ─────────────────────────
        results: dict = {}
        status(f"Processing {len(chapters)} chapter(s)…")
        for chapter_index, (title, text) in enumerate(chapters):
            if job_state["stop_event"].is_set():
                break
            try:
                result = process_chapter(chapter_index, title, text)
                results[result[0]] = result
            except StopIteration:
                job_state["status"] = "cancelled"
                log("\nStopped by user.")
                done(); return
            except Exception as error:
                log(f"\nChapter {chapter_index + 1} ({title[:40]}) failed: {error}")

        if job_state["stop_event"].is_set():
            log("\nStopped by user.")
            job_state["status"] = "cancelled"
            done(); return

        memlog("all chapters complete")

        # Collect results in chapter order, build merge list
        all_audio = []
        for chapter_index in sorted(results.keys()):
            _, filename, audio, _ = results[chapter_index]
            if filename:
                job_state["files"].append(filename)
            if settings["merge"] and audio is not None:
                all_audio.append(audio)
                if chapter_index < len(chapters) - 1:
                    all_audio.append(silence_array)

        # ── Merge all chapters into a single file ─────────────────────────────
        if settings["merge"] and all_audio and not job_state["stop_event"].is_set():
            status("Merging chapters…")
            log(f"Merging {len(chapters)} chapters…")
            full_audio   = np.concatenate(all_audio)
            wav_filename = f"{book_stem}_FULL.wav"
            wav_path     = os.path.join(settings["out_dir"], wav_filename)
            write_wav(wav_path, full_audio, SAMPLE_RATE)

            if engine == "chatterbox" and settings.get("chatterbox_speed", 1.0) != 1.0:
                try:
                    change_tempo(wav_path, settings["chatterbox_speed"])
                except Exception as error:
                    log(f"! Speed adjustment skipped (FULL): {error}")

            if settings.get("enhance"):
                try:
                    enhance_wav(wav_path)
                except Exception as error:
                    log(f"! Enhancement skipped (FULL): {error}")

            if settings.get("output_format") == "mp3":
                filename = f"{book_stem}_FULL.mp3"
                to_mp3(wav_path, os.path.join(settings["out_dir"], filename),
                       settings["bitrate"],
                       title="Full Audiobook",
                       album=settings.get("book_title_meta") or book_stem,
                       artist=settings.get("book_author_meta", ""),
                       cover_data=settings.get("cover_data"),
                       cover_mime=settings.get("cover_mime", "image/jpeg"))
                os.remove(wav_path)
            else:
                filename = wav_filename

            minutes = len(full_audio) / SAMPLE_RATE / 60
            log(f"Full audiobook saved — {minutes:.1f} min")
            job_state["files"].append(filename)
            _push({
                "type": "file", "filename": filename,
                "duration": len(full_audio) / SAMPLE_RATE, "chapter": 0,
                "title": "Full Audiobook (Merged)",
            })

        memlog("done")
        log(f"\nDone! {len(job_state['files'])} file(s) created.")
        job_state["status"] = "done"
        done()

    except Exception as error:
        import traceback
        log(f"\nError: {error}")
        log(traceback.format_exc())
        job_state["status"] = "error"
        done()
