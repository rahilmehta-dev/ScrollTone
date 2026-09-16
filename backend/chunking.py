"""Pure text/audio chunking helpers used by the conversion pipeline.

Split out of pipeline.py so they're independently testable (see
tests/test_chunker.py) without needing to close over a running job's state.
"""
import re

import numpy as np

from backend.audio import generate_breath


def split_sentences_into_chunks(text: str, chunk_size: int) -> list[str]:
    """Group sentences into ~chunk_size-char chunks without splitting mid-sentence."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks, current_chunk = [], ""
    for sentence in sentences:
        if len(current_chunk) + len(sentence) + 1 <= chunk_size:
            current_chunk = (current_chunk + " " + sentence).strip()
        else:
            if current_chunk:
                chunks.append(current_chunk)
            current_chunk = sentence
    if current_chunk:
        chunks.append(current_chunk)
    return chunks


def interleave_breaths(audio_chunks: list, sample_rate: int, rng: np.random.Generator) -> list:
    """Splice a short breath (or, sometimes, just a plain gap) between each
    chunk boundary — Chatterbox chunks otherwise get concatenated back-to-back
    with zero gap, which reads as rushed/robotic over a full chapter. Roughly
    half of boundaries get an audible breath; durations/intensities are
    randomized so it doesn't sound metronomic.
    """
    stitched = [audio_chunks[0]]
    for chunk in audio_chunks[1:]:
        if rng.random() < 0.55:
            stitched.append(np.zeros(
                int(sample_rate * rng.uniform(0.08, 0.18)), dtype=np.float32))
            stitched.append(generate_breath(
                sample_rate,
                duration=rng.uniform(0.25, 0.4),
                intensity=rng.uniform(0.02, 0.045),
                rng=rng,
            ))
            stitched.append(np.zeros(
                int(sample_rate * rng.uniform(0.05, 0.12)), dtype=np.float32))
        else:
            stitched.append(np.zeros(
                int(sample_rate * rng.uniform(0.15, 0.3)), dtype=np.float32))
        stitched.append(chunk)
    return stitched
