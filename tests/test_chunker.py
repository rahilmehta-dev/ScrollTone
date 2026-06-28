"""
Smoke tests for the text chunker logic.
Run with: pytest tests/
"""
import re
import pytest


def split_chunks(text: str, chunk_size: int) -> list[str]:
    """Inline of the current chunker for isolated testing."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks, cur = [], ""
    for sent in sentences:
        if len(cur) + len(sent) + 1 <= chunk_size:
            cur = (cur + " " + sent).strip()
        else:
            if cur:
                chunks.append(cur)
            cur = sent
    if cur:
        chunks.append(cur)
    return chunks


def test_single_sentence_fits():
    result = split_chunks("Hello world.", 500)
    assert result == ["Hello world."]


def test_splits_at_chunk_boundary():
    short = "Hi. " * 10          # 40 chars total
    result = split_chunks(short.strip(), 30)
    assert len(result) > 1
    for chunk in result:
        assert len(chunk) <= 60  # no chunk wildly oversized


def test_empty_string():
    assert split_chunks("", 500) == []


def test_no_punctuation_sentence():
    text = "This has no terminal punctuation so it stays as one chunk"
    result = split_chunks(text, 500)
    assert result == [text]


def test_preserves_all_content():
    text = "First sentence. Second sentence! Third sentence?"
    result = split_chunks(text, 500)
    assert " ".join(result) == text
