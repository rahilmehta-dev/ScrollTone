"""End-to-end proof test for all three TTS engines (Kokoro, Higgs Audio V2,
Chatterbox) using the ACTUAL production code paths — not throwaway
reimplementations. Standalone script, no UI/server needed.

Fixtures live in the repo:
    tests/fixtures/ebooks/         — every *.epub here is processed
    tests/fixtures/audio_samples/  — every audio file here is used as a cloning
                                      reference, one sample at a time, for engines
                                      that need one (Higgs, Chatterbox). Kokoro
                                      doesn't clone, so it only runs once per book.

For each book: Chapter 1 only by default (--chapter-index) — Chatterbox alone
runs at roughly 6x slower than realtime on CPU, and this is book x engine x
sample combinatorial, so a full book per combination would take very long.

--max-parallel runs all of a book/sample matrix's jobs through THREE
dedicated lanes, one per engine (kokoro / higgs / chatterbox), each lane
processing its own jobs strictly one-at-a-time but all three lanes running
concurrently with each other. This is deliberately NOT unbounded "run
everything at once" parallelism — this machine has exactly one GPU (Higgs),
and a single Chatterbox instance already saturates several CPU cores by
design, so running two jobs on the *same* engine concurrently would only
contend for the one resource that engine depends on, not speed anything up
(and given how surprising this app's MPS memory behavior has been, stacking
multiple GPU processes on it is not something to try casually). One lane per
engine is the actual maximum parallelism this hardware benefits from —
combined peak footprint is roughly 4+12+6.6=~23GB, well under a 64GB machine.

Per book, this runs:
    kokoro                              -> 1 run
    higgs   x each audio sample         -> N runs
    chatterbox x each audio sample      -> N runs

Requires:
  - Run with the MAIN venv (.venv) — runner.py only needs stdlib/numpy/
    soundfile, which are already base deps; it shells out to .venv-higgs /
    .venv-chatterbox internally for the heavy engines (see
    documentation/engines.md for setup).

Usage:
    .venv/bin/python scripts/test_all_engines.py

Output layout:
    tests/output/engines/<book_stem>/kokoro.wav                     (no sample used)
    tests/output/engines/<book_stem>/results_kokoro.json
    tests/output/engines/<book_stem>/<sample_stem>/higgs.wav         (one folder per
    tests/output/engines/<book_stem>/<sample_stem>/chatterbox.wav     sample, grouping
    tests/output/engines/<book_stem>/<sample_stem>/results_*.json     that sample's runs)
    tests/output/engines/SUMMARY.md                                  (one top-level table)
tests/output/ is gitignored — not meant to be committed.
"""
import argparse
import json
import re
import resource
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

SAMPLE_RATE = 24000
KOKORO_VOICE = "af_heart"  # best-graded Kokoro voice; doesn't need a reference clip
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg"}


def split_chunks(text: str, chunk_size: int = 500) -> list[str]:
    """Mirrors backend/pipeline.py's `_split_chunks` exactly — sentence-
    respecting, ~500-char chunks — so this test exercises the same chunking
    the real app would use for this text."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks, current = [], ""
    for sentence in sentences:
        if len(current) + len(sentence) + 1 <= chunk_size:
            current = (current + " " + sentence).strip()
        else:
            if current:
                chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)
    return chunks


def rss_gb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024.0 ** 3)


def run_kokoro(chunks: list[str], out_wav: Path) -> dict:
    import numpy as np
    import soundfile as sf
    import torch
    from kokoro import KPipeline

    device = "mps" if torch.backends.mps.is_available() else ("cuda" if torch.cuda.is_available() else "cpu")
    result = {
        "engine": "kokoro", "device_used": device, "voice": KOKORO_VOICE,
        "chunks_total": len(chunks), "chunks_processed": 0, "errors": [],
    }

    t0 = time.time()
    pipeline = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M", trf=False, device=device)
    result["model_load_time_s"] = time.time() - t0

    audio_pieces = []
    t0 = time.time()
    for i, chunk in enumerate(chunks):
        try:
            for _, _, audio in pipeline(chunk, voice=KOKORO_VOICE, speed=1.0):
                audio_pieces.append(audio)
            result["chunks_processed"] += 1
            print(f"  [kokoro] chunk {i + 1}/{len(chunks)} done", flush=True)
        except Exception as e:  # noqa: BLE001
            result["errors"].append(f"chunk {i}: {type(e).__name__}: {e}")
    result["synthesis_wall_time_s"] = time.time() - t0
    result["peak_rss_mb"] = rss_gb() * 1024

    if audio_pieces:
        full = np.concatenate(audio_pieces)
        sf.write(str(out_wav), full, SAMPLE_RATE)
        result["output_audio_duration_s"] = len(full) / SAMPLE_RATE
    return result


def run_subprocess_engine(engine: str, chunks: list[str], reference_wav: str, device: str, out_wav: Path) -> dict:
    from backend.engines.runner import synthesize_chapter, EngineNotInstalled
    import numpy as np
    import soundfile as sf

    result = {"engine": engine, "chunks_total": len(chunks), "reference_sample": Path(reference_wav).name}

    def on_progress(i, n):
        print(f"  [{engine}] chunk {i}/{n} done", flush=True)

    t0 = time.time()
    try:
        audio_arrays, engine_result = synthesize_chapter(
            engine, chunks, reference_wav, device, on_progress=on_progress,
        )
    except EngineNotInstalled as e:
        result["errors"] = [str(e)]
        result["chunks_processed"] = 0
        return result
    result["synthesis_wall_time_s"] = time.time() - t0
    result.update(engine_result)
    result["peak_rss_mb"] = rss_gb() * 1024  # this process only; workers run in their own subprocess

    if audio_arrays:
        full = np.concatenate(audio_arrays)
        sf.write(str(out_wav), full, SAMPLE_RATE)
        result["output_audio_duration_s"] = len(full) / SAMPLE_RATE
    return result


def run_one(engine: str, chunks: list[str], reference_wav: str | None, out_wav: Path) -> dict:
    t_start = time.time()
    if engine == "kokoro":
        result = run_kokoro(chunks, out_wav)
    else:
        device = "mps" if engine == "higgs" else "cpu"  # chatterbox_synth.py hardcodes cpu regardless
        result = run_subprocess_engine(engine, chunks, reference_wav, device, out_wav)
    result["total_wall_time_s"] = time.time() - t_start
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ebook-dir", default=str(REPO_ROOT / "tests" / "fixtures" / "ebooks"))
    parser.add_argument("--audio-sample-dir", default=str(REPO_ROOT / "tests" / "fixtures" / "audio_samples"))
    parser.add_argument("--chapter-index", type=int, default=0,
                         help="Which chapter to synthesize per book (default: first).")
    parser.add_argument("--min-ch-len", type=int, default=200)
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "tests" / "output" / "engines"))
    parser.add_argument("--engines", nargs="+", default=["kokoro", "higgs", "chatterbox"],
                         choices=["kokoro", "higgs", "chatterbox"])
    parser.add_argument("--max-parallel", action="store_true",
                         help="Run one dedicated lane per engine (kokoro/higgs/chatterbox), "
                              "all three lanes concurrent. Each lane stays strictly sequential "
                              "internally — see module docstring for why that's the real max "
                              "safe parallelism on a single-GPU machine.")
    args = parser.parse_args()

    ebook_dir = Path(args.ebook_dir)
    audio_dir = Path(args.audio_sample_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    books = sorted(ebook_dir.glob("*.epub"))
    samples = sorted(p for p in audio_dir.iterdir() if p.suffix.lower() in AUDIO_EXTS) if audio_dir.exists() else []

    if not books:
        print(f"No .epub files found in {ebook_dir}", file=sys.stderr)
        return 1
    needs_samples = any(e != "kokoro" for e in args.engines)
    if needs_samples and not samples:
        print(f"No audio samples found in {audio_dir} — higgs/chatterbox need at least one.", file=sys.stderr)
        return 1

    cloning_engines = [e for e in args.engines if e != "kokoro"]
    print(f"Books: {len(books)}   Audio samples: {len(samples)}   Engines: {args.engines}   "
          f"Mode: {'max-parallel (one lane per engine)' if args.max_parallel else 'sequential'}\n")

    from ebooklib import epub
    from backend.epub_parser import extract_chapters

    # ── Build the full job list upfront (one dict per synthesis run) ───────
    jobs = []  # each: {engine, book, sample_name, chunks, out_wav, results_path}
    for book_path in books:
        book_stem = re.sub(r"[^\w\s-]", "", book_path.stem)[:50].strip() or "book"
        book_out_dir = out_dir / book_stem
        book_out_dir.mkdir(parents=True, exist_ok=True)

        print(f"Reading {book_path.name} ...")
        try:
            book = epub.read_epub(str(book_path))
            chapters = extract_chapters(book, args.min_ch_len)
            if not chapters:
                print(f"  ! No chapters found, skipping.")
                continue
            title, text = chapters[args.chapter_index]
        except Exception as e:  # noqa: BLE001
            print(f"  ! Failed to read/parse: {e}")
            continue

        chunks = split_chunks(text)
        print(f"  Chapter {args.chapter_index}: {title!r} — {len(text):,} chars, {len(chunks)} chunks")
        (book_out_dir / "chapter_text.txt").write_text(text)

        if "kokoro" in args.engines:
            jobs.append({
                "engine": "kokoro", "book": book_stem, "sample_name": "-", "reference_wav": None,
                "chunks": chunks, "out_wav": book_out_dir / "kokoro.wav",
                "results_path": book_out_dir / "results_kokoro.json",
            })

        # higgs / chatterbox: once per audio sample, grouped into a folder
        # per sample so outputs from different samples never mix together:
        #   tests/output/engines/<book>/<sample_stem>/higgs.wav
        #   tests/output/engines/<book>/<sample_stem>/chatterbox.wav
        for sample_path in samples:
            sample_stem = re.sub(r"[^\w-]", "_", sample_path.stem)[:40]
            sample_out_dir = book_out_dir / sample_stem
            sample_out_dir.mkdir(parents=True, exist_ok=True)
            for engine in cloning_engines:
                jobs.append({
                    "engine": engine, "book": book_stem, "sample_name": sample_path.name,
                    "reference_wav": str(sample_path), "chunks": chunks,
                    "out_wav": sample_out_dir / f"{engine}.wav",
                    "results_path": sample_out_dir / f"results_{engine}.json",
                })

    def execute(job: dict) -> dict:
        label = f"{job['book']} / {job['sample_name']} / {job['engine']}"
        print(f"\n-- {label} --", flush=True)
        result = run_one(job["engine"], job["chunks"], job["reference_wav"], job["out_wav"])
        job["results_path"].write_text(json.dumps(result, indent=2))
        print(f"   [{label}] done in {result['total_wall_time_s']:.1f}s — "
              f"{result.get('chunks_processed', 0)}/{result['chunks_total']} chunks, "
              f"{len(result.get('errors', []))} error(s)", flush=True)
        return {"book": job["book"], "engine": job["engine"], "sample": job["sample_name"], **result}

    print(f"\n{len(jobs)} job(s) queued.\n")

    if args.max_parallel:
        # One single-worker lane per engine: each lane processes its jobs
        # strictly in order (never two jobs on the same engine at once), but
        # all engine lanes run concurrently with each other.
        engines_present = sorted({job["engine"] for job in jobs})
        with ThreadPoolExecutor(max_workers=len(engines_present)) as top_pool:
            def run_lane(engine: str) -> list[dict]:
                return [execute(job) for job in jobs if job["engine"] == engine]

            lane_futures = {engine: top_pool.submit(run_lane, engine) for engine in engines_present}
            all_rows = [row for engine in engines_present for row in lane_futures[engine].result()]
    else:
        all_rows = [execute(job) for job in jobs]

    # ── Summary ──────────────────────────────────────────────────────────
    lines = ["# All-engines proof test\n",
             "| Book | Engine | Sample | Chunks | Wall Time | Output Duration | Errors |",
             "|---|---|---|---|---|---|---|"]
    for row in all_rows:
        dur = row.get("output_audio_duration_s")
        lines.append(
            f"| {row['book']} | {row['engine']} | {row['sample']} | "
            f"{row.get('chunks_processed', 0)}/{row['chunks_total']} | "
            f"{row['total_wall_time_s']:.1f}s | {f'{dur:.1f}s' if dur else 'n/a'} | "
            f"{len(row.get('errors', []))} |"
        )
    summary = "\n".join(lines) + "\n"
    (out_dir / "SUMMARY.md").write_text(summary)
    print(f"\n\n{summary}")
    print(f"Results written to {out_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
