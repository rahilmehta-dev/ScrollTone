"""
Conversion job execution.

Responsibilities:
- convert_book() : full EPUB-to-audio pipeline for one book, run in a
                    background thread so it doesn't block the event loop.
                    Reads the book, then hands each chapter to a
                    ChapterProcessor (backend/chapter_processor.py) and
                    merges the results.

Chapters are processed one at a time, in order, on a single Kokoro pipeline.
"""
import asyncio
import os
import re
from pathlib import Path

from backend.epub_parser import (
    _find_epub_cover,
    extract_chapters,
    extract_chapters_from_text,
    get_book_metadata,
)
from backend.audio import write_wav, to_mp3, enhance_wav, change_tempo
from backend.voices import VoiceMapper, REGISTRY_FILENAME, SAMPLE_TEXT
from backend.job_events import JobEmitter
from backend.chapter_processor import ChapterProcessor

SAMPLE_RATE = 24000   # Kokoro output sample rate


def convert_book(job_state: dict, settings: dict, loop: asyncio.AbstractEventLoop) -> None:
    """Run a full conversion job.

    Pushed messages are JSON strings compatible with the SSE stream format:
        {"type": "log"|"status"|"progress"|"file"}

    Sends None to the queue when complete so the SSE generator can close.
    """
    emitter = JobEmitter(job_state, loop)
    log, status, prog, push, done = (
        emitter.log, emitter.status, emitter.progress, emitter.push, emitter.done,
    )

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
        push({"type": "ch_info",
              "chapters": [{"i": index, "title": chapter_title}
                           for index, (chapter_title, _) in enumerate(chapters)]})

        book_stem     = re.sub(r"[^\w\s-]", "", Path(settings["filename"]).stem)[:50]
        silence_array = np.zeros(int(SAMPLE_RATE * settings["silence"]), dtype=np.float32)
        breath_rng    = np.random.default_rng()

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

        chapter_processor = ChapterProcessor(
            job_state=job_state, settings=settings, emitter=emitter,
            engine=engine, pipeline=pipeline, voice_mapper=voice_mapper,
            registry_path=registry_path, ambience_enabled=ambience_enabled,
            ambience_log=ambience_log, ambience_log_path=ambience_log_path,
            book_stem=book_stem, total_chapters=len(chapters), breath_rng=breath_rng,
            sample_rate=SAMPLE_RATE,
        )

        # ── Execution — one chapter at a time, in order ─────────────────────────
        results: dict = {}
        status(f"Processing {len(chapters)} chapter(s)…")
        for chapter_index, (title, text) in enumerate(chapters):
            if job_state["stop_event"].is_set():
                break
            try:
                result = chapter_processor.process(chapter_index, title, text)
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
            push({
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


def run_preview_job(job_state: dict, settings: dict, loop: asyncio.AbstractEventLoop) -> None:
    """Preview & Tweak's synthesis job — same job/SSE machinery as
    convert_book() (routes/convert.py's /stream, /stop, /download all work
    unchanged on the job_id this produces), but for one synthetic "chapter":
    backend/voices.py SAMPLE_TEXT, instead of a real uploaded book.

    This is what lets the preview exercise Multi-voice/Ambient sound exactly
    as a real conversion would (same ChapterProcessor, same Ollama calls),
    and reports real chunk-by-chunk progress + honors a real server-side
    Stop — not just an abandoned client-side fetch.
    """
    emitter = JobEmitter(job_state, loop)
    log, status, push, done = emitter.log, emitter.status, emitter.push, emitter.done

    try:
        engine = settings["engine"]

        pipeline = None
        if engine == "kokoro" or settings.get("multi_voice"):
            status("Loading pipeline…")
            # Reuse the same per-language pipeline cache the quick voice-
            # audition button warms (backend/voices.py) — repeated Preview
            # clicks while tweaking parameters would otherwise reload the
            # whole model from scratch every single time.
            import backend.state as state
            lang = settings["lang_code"]
            with state._preview_lock:
                if lang not in state._preview_pipeline:
                    from kokoro import KPipeline
                    state._preview_pipeline[lang] = KPipeline(
                        lang_code=lang, repo_id="hexgrad/Kokoro-82M", device=settings["device"])
                pipeline = state._preview_pipeline[lang]
            log(f"Model ready  |  voice={settings['voice']}  speed={settings['speed']:.2f}×\n")
        else:
            log(f"Engine: {engine}  |  reference={Path(settings['reference_wav']).name}\n")
            for warning in settings.get("reference_warnings", []):
                log(f"   ! [reference] {warning}")

        import numpy as np
        breath_rng = np.random.default_rng()

        voice_mapper = VoiceMapper(settings["voice"]) if settings.get("multi_voice") else None
        registry_path = Path(settings["out_dir"]) / REGISTRY_FILENAME
        if voice_mapper:
            log(f"Multi-voice enabled  |  narrator={settings['voice']}  "
                f"model={settings['ollama_model']}  url={settings['ollama_url']}\n")

        ambience_enabled = bool(settings.get("ambience"))
        ambience_log_path = Path(settings["out_dir"]) / "ambience_cues.json"
        if ambience_enabled:
            log(f"Ambient sound enabled  |  model={settings['ollama_model']}  url={settings['ollama_url']}\n")

        chapter_processor = ChapterProcessor(
            job_state=job_state, settings=settings, emitter=emitter,
            engine=engine, pipeline=pipeline, voice_mapper=voice_mapper,
            registry_path=registry_path, ambience_enabled=ambience_enabled,
            ambience_log={}, ambience_log_path=ambience_log_path,
            book_stem="preview", total_chapters=1, breath_rng=breath_rng,
            sample_rate=SAMPLE_RATE, announce_title=False,
        )
        push({"type": "ch_info", "chapters": [{"i": 0, "title": "Preview Sample"}]})
        status("Synthesizing preview…")

        try:
            chapter_processor.process(0, "Preview Sample", SAMPLE_TEXT)
        except StopIteration:
            log("\nStopped by user.")
            job_state["status"] = "cancelled"
            done()
            return

        log("\nDone.")
        job_state["status"] = "done"
        done()

    except Exception as error:
        import traceback
        log(f"\nError: {error}")
        log(traceback.format_exc())
        job_state["status"] = "error"
        done()
