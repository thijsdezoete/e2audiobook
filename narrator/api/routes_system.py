import asyncio
import logging
from collections import deque

from fastapi import APIRouter, Query

from narrator.health import state as health_state

router = APIRouter()
log = logging.getLogger(__name__)

log_buffer: deque[dict] = deque(maxlen=10000)


class BufferHandler(logging.Handler):
    def emit(self, record):
        log_buffer.append({
            "timestamp": self.format(record).split("]")[0].lstrip("[") if "]" in self.format(record) else "",
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        })


_handler = BufferHandler()
_handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
logging.getLogger("narrator").addHandler(_handler)


@router.get("/health")
async def health():
    return health_state.to_dict()


@router.get("/version")
async def version():
    return {"version": "0.3.0"}


@router.post("/scan")
async def scan_library():
    from narrator.app import settings
    from narrator.core.calibre_reader import get_reader

    reader = get_reader(settings)
    books = await asyncio.to_thread(reader.list_books)
    return {"books_found": len(books)}


@router.get("/logs")
async def get_logs(
    level: str = Query(default="", description="Filter by log level"),
    search: str = Query(default="", description="Search in messages"),
    limit: int = Query(default=200, ge=1, le=10000),
):
    entries = list(log_buffer)
    if level:
        entries = [e for e in entries if e["level"] == level.upper()]
    if search:
        sl = search.lower()
        entries = [e for e in entries if sl in e["message"].lower()]
    return {"logs": entries[-limit:], "total": len(entries)}


@router.get("/stats")
async def get_stats():
    from narrator.app import db
    from narrator.db.models import JobStatus
    from narrator.job_queue import JobQueue

    queue = JobQueue(db)
    summary = await asyncio.to_thread(queue.queue_summary)
    completed = await asyncio.to_thread(queue.list_jobs, status=JobStatus.COMPLETE)

    total_duration = sum(j.duration_seconds or 0 for j in completed)
    total_size = sum(j.file_size_bytes or 0 for j in completed)

    authors: dict[str, int] = {}
    voices: dict[str, int] = {}
    for j in completed:
        authors[j.author] = authors.get(j.author, 0) + 1
        voices[j.voice] = voices.get(j.voice, 0) + 1

    top_authors = sorted(authors.items(), key=lambda x: x[1], reverse=True)[:10]
    voice_usage = sorted(voices.items(), key=lambda x: x[1], reverse=True)

    return {
        "queue": summary,
        "completed_books": len(completed),
        "total_duration_seconds": total_duration,
        "total_size_bytes": total_size,
        "top_authors": [{"author": a, "count": c} for a, c in top_authors],
        "voice_usage": [{"voice": v, "count": c} for v, c in voice_usage],
    }
