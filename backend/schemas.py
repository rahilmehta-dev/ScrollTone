"""
Pydantic response models for the ScrollTone API.
"""
from pydantic import BaseModel


class ChapterInfo(BaseModel):
    index: int
    title: str
    chars: int


class ChapterListResponse(BaseModel):
    chapters: list[ChapterInfo]


class ConvertResponse(BaseModel):
    batch_id: str
    job_ids:  list[str]
    titles:   list[str]


class JobFile(BaseModel):
    filename: str
    duration: float
    chapter:  int
    title:    str


class StopResponse(BaseModel):
    status: str
