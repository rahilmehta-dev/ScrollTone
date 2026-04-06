"""
Conversion job execution.

Responsibilities:
- _worker()          : full EPUB-to-audio pipeline run in a daemon thread
- process_chapter()  : per-chapter synthesis (nested closure inside _worker)

Each call to _worker creates its own pipeline pool so multiple concurrent jobs
never share Kokoro instances.
"""
import asyncio
import json
import os
import queue as thread_queue
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import core.state as state
from core.epub_parser import _find_epub_cover, extract_chapters, get_book_metadata
from core.audio_utils import write_wav, to_mp3, enhance_wav
from core.speaker_attribution import VoiceMapper, attribute_speakers

SAMPLE_RATE = 24000   # Kokoro output sample rate


def _worker(job: dict, s: dict, loop: asyncio.AbstractEventLoop) -> None:
    """Run a full conversion job.

    Pushed messages are JSON strings compatible with the SSE stream format:
        {"type": "log"|"status"|"progress"|"file"}

    Sends None to the queue when complete so the SSE generator can close.
    """

    # ── Push helpers ──────────────────────────────────────────────────────────
    def _push(data: dict):
        loop.call_soon_threadsafe(job["queue"].put_nowait, json.dumps(data))

    def log(msg: str):    _push({"type": "log",      "msg": msg})
    def status(msg: str): _push({"type": "status",   "msg": msg})
    def prog(v, lbl=""):  _push({"type": "progress", "value": v, "label": lbl})
    def done():           loop.call_soon_threadsafe(job["queue"].put_nowait, None)

    try:
        import psutil
        from kokoro import KPipeline
        from ebooklib import epub
        import numpy as np

        _proc = psutil.Process(os.getpid())

        def memlog(label: str = ""):
            rss   = _proc.memory_info().rss / 1024**3
            vm    = psutil.virtual_memory()
            msg   = (f"[MEM] {label}  "
                     f"process={rss:.2f}GB  "
                     f"system={vm.used/1024**3:.2f}/{vm.total/1024**3:.2f}GB  "
                     f"avail={vm.available/1024**3:.2f}GB")
            log(msg)
            print(msg, flush=True)

        num_w = s["num_workers"]
        memlog("startup")

        # Auto-cap workers based on available RAM (each pipeline ≈ 0.9 GB)
        avail_gb = psutil.virtual_memory().available / 1024 ** 3
        max_safe  = max(1, int((avail_gb - 2.0) / 0.9))
        if num_w > max_safe:
            log(f"[MEM] Reducing workers {num_w} → {max_safe} "
                f"(only {avail_gb:.1f} GB available; "
                f"need ~{max_safe * 0.9 + 2:.1f} GB for {max_safe})")
            num_w = max_safe

        # Load per-job pipeline pool
        status(f"Loading {num_w} pipeline instance(s)…")
        log(f"Initializing {num_w} pipeline(s)  lang={s['lang_code']}  trf={s['trf']}")
        pool: thread_queue.Queue = thread_queue.Queue()
        for w in range(num_w):
            pool.put(KPipeline(lang_code=s["lang_code"],
                               repo_id="hexgrad/Kokoro-82M",
                               trf=s["trf"], device=s["device"]))
            memlog(f"after pipeline {w + 1}/{num_w} loaded")
        log(f"Model(s) ready  |  voice={s['voice']}  speed={s['speed']:.2f}×\n")

        # Read and parse EPUB
        status("Reading EPUB…")
        log(f"Reading: {s['filename']}")
        book = epub.read_epub(s["epub"])

        metadata = get_book_metadata(book)
        s["book_title_meta"]  = metadata["title"]
        s["book_author_meta"] = metadata["author"]
        s["cover_data"], s["cover_mime"] = _find_epub_cover(book)
        if s["cover_data"]:
            log(f"Cover image found ({len(s['cover_data']) // 1024} KB, {s['cover_mime']})")
        else:
            log("No cover image found in EPUB")

        chapters = extract_chapters(book, s["min_ch_len"])

        sel = s.get("chapter_indices")
        if sel is not None:
            sel_set  = set(sel)
            chapters = [ch for i, ch in enumerate(chapters) if i in sel_set]

        if not chapters:
            log("No chapters found in EPUB.")
            job["status"] = "error"
            done(); return

        log(f"Found {len(chapters)} chapters\n")
        # Seed the chapter progress grid in the UI
        _push({"type": "ch_info",
               "chapters": [{"i": i, "title": t} for i, (t, _) in enumerate(chapters)]})

        book_stem   = re.sub(r"[^\w\s-]", "", Path(s["filename"]).stem)[:50]
        silence_arr = np.zeros(int(SAMPLE_RATE * s["silence"]), dtype=np.float32)

        done_lock  = threading.Lock()
        done_count = [0]

        # ── Voice mapper (shared across all chapters for consistency) ─────────
        voice_mapper = VoiceMapper(s["voice"]) if s.get("multi_voice") else None
        if voice_mapper:
            log(f"Multi-voice enabled  |  narrator={s['voice']}  "
                f"model={s['ollama_model']}  url={s['ollama_url']}\n")

        def _split_chunks(text: str) -> list[str]:
            sentences = re.split(r"(?<=[.!?])\s+", text)
            chunks, cur = [], ""
            for sent in sentences:
                if len(cur) + len(sent) + 1 <= s["chunk_size"]:
                    cur = (cur + " " + sent).strip()
                else:
                    if cur: chunks.append(cur)
                    cur = sent
            if cur: chunks.append(cur)
            return chunks

        # ── Per-chapter worker (runs inside ThreadPoolExecutor) ───────────────
        def process_chapter(ch_i, title, text):
            pipeline = pool.get()   # borrow — blocks if all workers are busy
            try:
                if job["stop_event"].is_set():
                    raise StopIteration

                ch_num = ch_i + 1
                n      = len(chapters)
                log(f"── Chapter {ch_num}/{n}: {title}")
                log(f"   {len(text):,} chars")

                ch_audio = []

                # ── Chapter title announcement ────────────────────────────────
                # Prepend: 0.5 s silence → spoken title → 0.75 s silence
                try:
                    title_frames = []
                    for _, _, audio in pipeline(title, voice=s["voice"], speed=s["speed"]):
                        title_frames.append(audio)
                    if title_frames:
                        ch_audio.append(np.zeros(int(SAMPLE_RATE * 0.5), dtype=np.float32))
                        ch_audio.extend(title_frames)
                        ch_audio.append(np.zeros(int(SAMPLE_RATE * 0.75), dtype=np.float32))
                except Exception as _te:
                    log(f"   ! Title announcement skipped: {_te}")

                if voice_mapper:
                    # ── Multi-voice path ──────────────────────────────────────
                    # One LLM call per chapter to attribute all dialogue
                    log(f"   [Ollama] → {s['ollama_model']}  "
                        f"({len(text):,} chars, {s['ollama_url']})")
                    try:
                        segments = attribute_speakers(
                            text, s["ollama_url"], s["ollama_model"]
                        )
                        dialogue = [sg for sg in segments if sg["type"] == "dialogue"]
                        narration = [sg for sg in segments if sg["type"] == "narration"]
                        log(f"   [Ollama] ← {len(segments)} segments  "
                            f"({len(dialogue)} dialogue, {len(narration)} narration)")
                        # Log each new character assignment
                        for seg in dialogue:
                            spk = seg.get("speaker")
                            gen = seg.get("gender") or "?"
                            if spk:
                                voice = voice_mapper.get_voice(spk, seg.get("gender"))
                                preview = seg["text"][:60].replace("\n", " ")
                                log(f"   [Ollama]   {spk} ({gen}) → {voice}  \"{preview}…\"")
                        log(f"   Characters so far: {voice_mapper.summary()}")
                    except Exception as e:
                        log(f"   [Ollama] ! Attribution failed: {e}")
                        log(f"   [Ollama] ! Falling back to single voice ({s['voice']})")
                        segments = [{"type": "narration", "text": text,
                                     "speaker": None, "gender": None}]

                    # Flatten segments → sub-chunks with per-chunk voice
                    voice_chunks = []
                    for seg in segments:
                        seg_text = seg.get("text", "").strip()
                        if not seg_text:
                            continue
                        voice = voice_mapper.get_voice(seg.get("speaker"), seg.get("gender"))
                        for sub in _split_chunks(seg_text):
                            voice_chunks.append((voice, sub))

                    n_chunks      = len(voice_chunks)
                    prog_interval = max(1, n_chunks // 20)
                    _push({"type": "ch_start", "ch_i": ch_i, "chunks": n_chunks})

                    for c_i, (voice, chunk) in enumerate(voice_chunks):
                        if job["stop_event"].is_set():
                            raise StopIteration
                        try:
                            for _, _, audio in pipeline(chunk, voice=voice, speed=s["speed"]):
                                ch_audio.append(audio)
                        except StopIteration:
                            raise
                        except Exception as e:
                            log(f"   ! Ch{ch_num} chunk {c_i + 1} skipped: {e}")
                        if (c_i + 1) % prog_interval == 0 or c_i == n_chunks - 1:
                            _push({"type": "ch_prog", "ch_i": ch_i,
                                   "pct": round((c_i + 1) / n_chunks, 3)})

                else:
                    # ── Single-voice path (original) ──────────────────────────
                    chunks        = _split_chunks(text)
                    n_chunks      = len(chunks)
                    prog_interval = max(1, n_chunks // 20)
                    log(f"   {n_chunks} chunks")
                    _push({"type": "ch_start", "ch_i": ch_i, "chunks": n_chunks})

                    for c_i, chunk in enumerate(chunks):
                        if job["stop_event"].is_set():
                            raise StopIteration
                        try:
                            for _, _, audio in pipeline(chunk, voice=s["voice"], speed=s["speed"]):
                                ch_audio.append(audio)
                        except StopIteration:
                            raise
                        except Exception as e:
                            log(f"   ! Ch{ch_num} chunk {c_i + 1} skipped: {e}")
                        if (c_i + 1) % prog_interval == 0 or c_i == n_chunks - 1:
                            _push({"type": "ch_prog", "ch_i": ch_i,
                                   "pct": round((c_i + 1) / n_chunks, 3)})

                if not ch_audio:
                    log(f"   (no audio generated)\n")
                    _push({"type": "ch_skip", "ch_i": ch_i})
                    return (ch_i, None, None, 0.0)

                combined   = np.concatenate(ch_audio)
                safe_title = re.sub(r"[^\w\s-]", "", title)[:35].strip()
                fname_wav  = f"{book_stem}_{safe_title}.wav"
                out_wav    = os.path.join(s["out_dir"], fname_wav)
                write_wav(out_wav, combined, SAMPLE_RATE)

                if s.get("enhance"):
                    try:
                        enhance_wav(out_wav)
                    except Exception as e:
                        log(f"   ! Enhancement skipped (Ch{ch_num}): {e}")

                if s.get("output_format") == "mp3":
                    fname = f"{book_stem}_{safe_title}.mp3"
                    to_mp3(out_wav, os.path.join(s["out_dir"], fname),
                           s["bitrate"],
                           title=title,
                           album=s.get("book_title_meta") or book_stem,
                           artist=s.get("book_author_meta", ""),
                           track=ch_num,
                           cover_data=s.get("cover_data"),
                           cover_mime=s.get("cover_mime", "image/jpeg"))
                    os.remove(out_wav)
                else:
                    fname = fname_wav

                dur = len(combined) / SAMPLE_RATE
                log(f"   Saved: {fname}  ({dur:.1f}s)\n")

                with done_lock:
                    done_count[0] += 1
                    frac = done_count[0] / n
                prog(frac, f"{done_count[0]}/{n} chapters done")

                _push({"type": "file", "filename": fname,
                       "duration": dur, "chapter": ch_num, "title": title})
                return (ch_i, fname, combined, dur)

            finally:
                pool.put(pipeline)   # always return — prevents pool starvation

        # ── Parallel execution ────────────────────────────────────────────────
        status(f"Processing {len(chapters)} chapter(s) with {num_w} worker(s)…")
        futures_map: dict = {}
        results:     dict = {}

        with ThreadPoolExecutor(max_workers=num_w) as executor:
            for ch_i, (title, text) in enumerate(chapters):
                if job["stop_event"].is_set():
                    break
                futures_map[executor.submit(process_chapter, ch_i, title, text)] = (ch_i, title)

            for fut in as_completed(futures_map):
                ch_i, title = futures_map[fut]
                try:
                    r = fut.result()
                    results[r[0]] = r
                except StopIteration:
                    for pending in futures_map:
                        pending.cancel()
                    job["status"] = "cancelled"
                    log("\nStopped by user.")
                    done(); return
                except Exception as exc:
                    log(f"\nChapter {ch_i + 1} ({title[:40]}) failed: {exc}")

        if job["stop_event"].is_set():
            log("\nStopped by user.")
            job["status"] = "cancelled"
            done(); return

        memlog("all chapters complete")

        # Collect results in chapter order, build merge list
        all_audio = []
        for ch_i in sorted(results.keys()):
            _, fname, audio, _ = results[ch_i]
            if fname:
                job["files"].append(fname)
            if s["merge"] and audio is not None:
                all_audio.append(audio)
                if ch_i < len(chapters) - 1:
                    all_audio.append(silence_arr)

        # ── Merge all chapters into a single file ─────────────────────────────
        if s["merge"] and all_audio and not job["stop_event"].is_set():
            status("Merging chapters…")
            log(f"Merging {len(chapters)} chapters…")
            full      = np.concatenate(all_audio)
            fname_wav = f"{book_stem}_FULL.wav"
            out_wav   = os.path.join(s["out_dir"], fname_wav)
            write_wav(out_wav, full, SAMPLE_RATE)

            if s.get("enhance"):
                try:
                    enhance_wav(out_wav)
                except Exception as e:
                    log(f"! Enhancement skipped (FULL): {e}")

            if s.get("output_format") == "mp3":
                fname = f"{book_stem}_FULL.mp3"
                to_mp3(out_wav, os.path.join(s["out_dir"], fname),
                       s["bitrate"],
                       title="Full Audiobook",
                       album=s.get("book_title_meta") or book_stem,
                       artist=s.get("book_author_meta", ""),
                       cover_data=s.get("cover_data"),
                       cover_mime=s.get("cover_mime", "image/jpeg"))
                os.remove(out_wav)
            else:
                fname = fname_wav

            mins = len(full) / SAMPLE_RATE / 60
            log(f"Full audiobook saved — {mins:.1f} min")
            job["files"].append(fname)
            _push({
                "type": "file", "filename": fname,
                "duration": len(full) / SAMPLE_RATE, "chapter": 0,
                "title": "Full Audiobook (Merged)",
            })

        memlog("done")
        log(f"\nDone! {len(job['files'])} file(s) created.")
        job["status"] = "done"
        done()

    except Exception as exc:
        import traceback
        log(f"\nError: {exc}")
        log(traceback.format_exc())
        job["status"] = "error"
        done()
