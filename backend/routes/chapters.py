"""
GET-list-of-chapters route.

POST /chapters — parse an uploaded EPUB/.txt and return its chapter titles
                  + char counts, so the UI can offer chapter selection before
                  a conversion actually starts. Also returns title/author/cover
                  when available (EPUB only) — the same metadata the convert
                  pipeline already extracts for MP3 tagging, surfaced here so
                  the setup screen can show the actual book instead of just a
                  filename.
"""
import base64
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from backend.epub_parser import (
    _find_epub_cover,
    extract_chapters,
    extract_chapters_from_text,
    get_book_metadata,
)

router = APIRouter()


@router.post("/chapters")
async def list_chapters(
    file:       UploadFile = File(...),
    min_ch_len: int        = Form(200),
):
    """Parse an EPUB or .txt upload and return its chapter list (title + char count)."""
    data = await file.read()
    is_txt = Path(file.filename or "").suffix.lower() == ".txt"

    if is_txt:
        try:
            text     = data.decode("utf-8", errors="ignore")
            chapters = extract_chapters_from_text(text, min_ch_len)
            return {
                "chapters": [
                    {"index": index, "title": title, "chars": len(text)}
                    for index, (title, text) in enumerate(chapters)
                ]
            }
        except Exception as error:
            raise HTTPException(status_code=500, detail=str(error))

    tmp_path = None
    try:
        from ebooklib import epub
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        book     = epub.read_epub(tmp_path)
        chapters = extract_chapters(book, min_ch_len)
        meta     = get_book_metadata(book)

        cover = None
        try:
            cover_data, cover_mime = _find_epub_cover(book)
            if cover_data:
                cover = {"mime": cover_mime, "data": base64.b64encode(cover_data).decode("ascii")}
        except Exception:
            pass  # Cover art is a nice-to-have for the preview — never block chapter listing on it.

        return {
            "chapters": [
                {"index": index, "title": title, "chars": len(text)}
                for index, (title, text) in enumerate(chapters)
            ],
            "title":  meta.get("title", ""),
            "author": meta.get("author", ""),
            "cover":  cover,
        }
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))
    finally:
        if tmp_path:
            try: os.unlink(tmp_path)
            except OSError: pass
