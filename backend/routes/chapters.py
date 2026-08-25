"""
GET-list-of-chapters route.

POST /chapters — parse an uploaded EPUB/.txt and return its chapter titles
                  + char counts, so the UI can offer chapter selection before
                  a conversion actually starts.
"""
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from backend.epub_parser import extract_chapters, extract_chapters_from_text

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
        return {
            "chapters": [
                {"index": index, "title": title, "chars": len(text)}
                for index, (title, text) in enumerate(chapters)
            ]
        }
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))
    finally:
        if tmp_path:
            try: os.unlink(tmp_path)
            except OSError: pass
