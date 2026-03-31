#!/usr/bin/env python3
"""
EPUB to Audiobook Converter using Kokoro TTS
Usage: python3 epub_to_audiobook.py <file.epub> [options]
"""

import argparse
import os
import sys
import re
from pathlib import Path

def install_deps():
    import subprocess
    pkgs = ["kokoro", "soundfile", "ebooklib", "beautifulsoup4", "numpy"]
    for pkg in pkgs:
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])

def extract_chapters(epub_path):
    """Extract text chapters from EPUB file."""
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup

    book = epub.read_epub(epub_path)
    chapters = []

    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            soup = BeautifulSoup(item.get_content(), 'html.parser')

            # Remove script/style tags
            for tag in soup(['script', 'style', 'head']):
                tag.decompose()

            text = soup.get_text(separator=' ')
            # Clean up whitespace
            text = re.sub(r'\s+', ' ', text).strip()

            # Skip very short sections (likely metadata/nav)
            if len(text) > 200:
                # Try to get chapter title
                title_tag = soup.find(['h1', 'h2', 'h3'])
                title = title_tag.get_text().strip() if title_tag else f"Section {len(chapters)+1}"
                chapters.append({'title': title, 'text': text})

    return chapters

def chunk_text(text, max_chars=500):
    """Split text into chunks at sentence boundaries."""
    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks = []
    current = ""

    for sentence in sentences:
        if len(current) + len(sentence) < max_chars:
            current += " " + sentence
        else:
            if current.strip():
                chunks.append(current.strip())
            current = sentence

    if current.strip():
        chunks.append(current.strip())

    return chunks

def convert_to_audio(epub_path, output_dir, voice='af_heart', speed=1.0, merge=True):
    """Main conversion function."""
    from kokoro import KPipeline
    import soundfile as sf
    import numpy as np

    print(f"\n📚 Reading EPUB: {epub_path}")
    chapters = extract_chapters(epub_path)
    print(f"✅ Found {len(chapters)} chapters\n")

    if not chapters:
        print("❌ No text content found in EPUB.")
        return

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"🎙️  Loading Kokoro TTS (voice: {voice})...")
    pipeline = KPipeline(lang_code='a')

    book_name = Path(epub_path).stem
    all_audio = []
    chapter_files = []

    for i, chapter in enumerate(chapters):
        print(f"\n📖 Chapter {i+1}/{len(chapters)}: {chapter['title']}")
        print(f"   {len(chapter['text'])} characters")

        chapter_audio = []
        chunks = chunk_text(chapter['text'])

        for j, chunk in enumerate(chunks):
            print(f"   🔊 Processing chunk {j+1}/{len(chunks)}...", end='\r')
            try:
                generator = pipeline(chunk, voice=voice, speed=speed)
                for _, _, audio in generator:
                    chapter_audio.append(audio)
            except Exception as e:
                print(f"\n   ⚠️  Skipped chunk: {e}")

        if chapter_audio:
            chapter_array = np.concatenate(chapter_audio)

            # Save individual chapter
            chapter_filename = output_dir / f"{book_name}_ch{i+1:02d}_{chapter['title'][:30].replace(' ','_')}.wav"
            sf.write(str(chapter_filename), chapter_array, 24000)
            chapter_files.append(str(chapter_filename))
            print(f"\n   ✅ Saved: {chapter_filename.name}")

            if merge:
                all_audio.append(chapter_array)
                # Add 1 second silence between chapters
                silence = np.zeros(24000)
                all_audio.append(silence)

    # Save merged full audiobook
    if merge and all_audio:
        merged_path = output_dir / f"{book_name}_FULL.wav"
        print(f"\n🎧 Merging all chapters into: {merged_path.name}")
        merged = np.concatenate(all_audio)
        sf.write(str(merged_path), merged, 24000)
        print(f"✅ Full audiobook saved: {merged_path}")

    print(f"\n🎉 Done! Output saved to: {output_dir}/")
    print(f"   • {len(chapter_files)} chapter files")
    if merge:
        print(f"   • 1 merged full audiobook")

def main():
    parser = argparse.ArgumentParser(description='Convert EPUB to Audiobook using Kokoro TTS')
    parser.add_argument('epub', help='Path to the EPUB file')
    parser.add_argument('--output', '-o', default='audiobook_output', help='Output directory (default: audiobook_output)')
    parser.add_argument('--voice', '-v', default='af_heart',
                        choices=['af_heart', 'af_bella', 'am_adam', 'am_michael'],
                        help='Voice to use (default: af_heart)')
    parser.add_argument('--speed', '-s', type=float, default=1.0, help='Speech speed (default: 1.0)')
    parser.add_argument('--no-merge', action='store_true', help='Skip creating merged full audiobook')
    args = parser.parse_args()

    if not os.path.exists(args.epub):
        print(f"❌ File not found: {args.epub}")
        sys.exit(1)

    print("📦 Installing dependencies...")
    install_deps()

    convert_to_audio(
        epub_path=args.epub,
        output_dir=args.output,
        voice=args.voice,
        speed=args.speed,
        merge=not args.no_merge
    )

if __name__ == '__main__':
    main()