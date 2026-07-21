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
from backend.epub_parser import _find_epub_cover, extract_chapters, get_book_metadata
from backend.audio import write_wav, to_mp3, enhance_wav
from backend.voices import VoiceMapper
from backend.attribution import attribute_speakers

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

        # Load the pipeline for this job
        status("Loading pipeline…")
        log(f"Initializing pipeline  lang={settings['lang_code']}  trf={settings['trf']}")
        pipeline = KPipeline(lang_code=settings["lang_code"],
                              repo_id="hexgrad/Kokoro-82M",
                              trf=settings["trf"], device=settings["device"])
        memlog("after pipeline loaded")
        log(f"Model ready  |  voice={settings['voice']}  speed={settings['speed']:.2f}×\n")

        # Read and parse EPUB
        status("Reading EPUB…")
        log(f"Reading: {settings['filename']}")
        book = epub.read_epub(settings["epub"])

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
            log("No chapters found in EPUB.")
            job_state["status"] = "error"
            done(); return

        log(f"Found {len(chapters)} chapters\n")
        # Seed the chapter progress grid in the UI
        _push({"type": "ch_info",
               "chapters": [{"i": index, "title": chapter_title}
                            for index, (chapter_title, _) in enumerate(chapters)]})

        book_stem     = re.sub(r"[^\w\s-]", "", Path(settings["filename"]).stem)[:50]
        silence_array = np.zeros(int(SAMPLE_RATE * settings["silence"]), dtype=np.float32)

        done_count = 0

        # ── Voice mapper (shared across all chapters for consistency) ─────────
        voice_mapper = VoiceMapper(settings["voice"]) if settings.get("multi_voice") else None
        if voice_mapper:
            log(f"Multi-voice enabled  |  narrator={settings['voice']}  "
                f"model={settings['ollama_model']}  url={settings['ollama_url']}\n")

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

            # ── Chapter title announcement ────────────────────────────────
            # Prepend: 0.5 s silence → spoken title → 0.75 s silence
            try:
                title_frames = []
                for _, _, audio in pipeline(title, voice=settings["voice"], speed=settings["speed"]):
                    title_frames.append(audio)
                if title_frames:
                    chapter_audio.append(np.zeros(int(SAMPLE_RATE * 0.5), dtype=np.float32))
                    chapter_audio.extend(title_frames)
                    chapter_audio.append(np.zeros(int(SAMPLE_RATE * 0.75), dtype=np.float32))
            except Exception as title_error:
                log(f"   ! Title announcement skipped: {title_error}")

            if voice_mapper:
                # ── Multi-voice path ──────────────────────────────────────
                # One LLM call per chapter to attribute all dialogue
                log(f"   [Ollama] → {settings['ollama_model']}  "
                    f"({len(text):,} chars, {settings['ollama_url']})")
                try:
                    segments = attribute_speakers(
                        text, settings["ollama_url"], settings["ollama_model"]
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
                # ── Single-voice path (original) ──────────────────────────
                chunks             = _split_chunks(text)
                total_chunks       = len(chunks)
                progress_interval  = max(1, total_chunks // 20)
                log(f"   {total_chunks} chunks")
                _push({"type": "ch_start", "ch_i": chapter_index, "chunks": total_chunks})

                for chunk_index, chunk in enumerate(chunks):
                    if job_state["stop_event"].is_set():
                        raise StopIteration
                    try:
                        for _, _, audio in pipeline(chunk, voice=settings["voice"], speed=settings["speed"]):
                            chapter_audio.append(audio)
                    except StopIteration:
                        raise
                    except Exception as error:
                        log(f"   ! Ch{chapter_number} chunk {chunk_index + 1} skipped: {error}")
                    if (chunk_index + 1) % progress_interval == 0 or chunk_index == total_chunks - 1:
                        _push({"type": "ch_prog", "ch_i": chapter_index,
                               "pct": round((chunk_index + 1) / total_chunks, 3)})

            if not chapter_audio:
                log(f"   (no audio generated)\n")
                _push({"type": "ch_skip", "ch_i": chapter_index})
                return (chapter_index, None, None, 0.0)

            combined_audio = np.concatenate(chapter_audio)
            safe_title     = re.sub(r"[^\w\s-]", "", title)[:35].strip()
            wav_filename   = f"{book_stem}_{safe_title}.wav"
            wav_path       = os.path.join(settings["out_dir"], wav_filename)
            write_wav(wav_path, combined_audio, SAMPLE_RATE)

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
