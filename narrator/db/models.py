from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class JobStatus(StrEnum):
    PENDING = "pending"
    EXTRACTING = "extracting"
    SYNTHESIZING = "synthesizing"
    BUILDING = "building"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass
class Job:
    id: int
    calibre_book_id: int
    title: str
    author: str
    voice: str
    status: JobStatus
    series: str | None = None
    series_index: float | None = None
    chapters_total: int = 0
    chapters_done: int = 0
    error_message: str | None = None
    epub_path: str | None = None
    output_path: str | None = None
    queue_position: int | None = None
    duration_seconds: int | None = None
    file_size_bytes: int | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
