import asyncio
import logging

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from narrator.health import state as health_state

router = APIRouter()
log = logging.getLogger(__name__)

_subscribers: list[asyncio.Queue] = []


def publish_event(event_type: str, data: dict):
    import json
    message = {"event": event_type, "data": json.dumps(data)}
    dead = []
    for q in _subscribers:
        try:
            q.put_nowait(message)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _subscribers.remove(q)


@router.get("")
async def queue_state():
    from narrator.app import db, worker
    from narrator.job_queue import JobQueue

    queue = JobQueue(db)
    summary = await asyncio.to_thread(queue.queue_summary)

    active = None
    if worker.current_job_id:
        try:
            job = await asyncio.to_thread(queue.get_job, worker.current_job_id)
            active = _job_dict(job)
        except ValueError:
            pass

    pending = await asyncio.to_thread(queue.list_jobs, status="pending")
    return {
        "paused": health_state.queue_paused,
        "summary": summary,
        "active": active,
        "pending": [_job_dict(j) for j in pending],
    }


@router.post("/pause")
async def pause_queue():
    health_state.queue_paused = True
    publish_event("queue_paused", {})
    return {"paused": True}


@router.post("/resume")
async def resume_queue():
    health_state.queue_paused = False
    publish_event("queue_resumed", {})
    return {"paused": False}


@router.delete("/{job_id}")
async def cancel_job(job_id: int):
    from narrator.app import db
    from narrator.job_queue import JobQueue

    queue = JobQueue(db)
    try:
        await asyncio.to_thread(queue.cancel_job, job_id)
        return {"cancelled": True}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/{job_id}/retry")
async def retry_job(job_id: int):
    from narrator.app import db
    from narrator.job_queue import JobQueue

    queue = JobQueue(db)
    try:
        job = await asyncio.to_thread(queue.retry_job, job_id)
        return _job_dict(job)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.patch("/reorder")
async def reorder_queue(body: dict):
    from narrator.app import db
    from narrator.job_queue import JobQueue

    job_ids = body.get("job_ids", [])
    if not job_ids:
        raise HTTPException(status_code=400, detail="job_ids required")
    queue = JobQueue(db)
    await asyncio.to_thread(queue.reorder, job_ids)
    return {"reordered": True}


@router.get("/events")
async def queue_events():
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subscribers.append(q)

    async def event_generator():
        try:
            while True:
                message = await q.get()
                yield message
        except asyncio.CancelledError:
            pass
        finally:
            if q in _subscribers:
                _subscribers.remove(q)

    return EventSourceResponse(event_generator())


def _job_dict(job) -> dict:
    return {
        "id": job.id,
        "calibre_book_id": job.calibre_book_id,
        "title": job.title,
        "author": job.author,
        "voice": job.voice,
        "status": job.status.value,
        "series": job.series,
        "chapters_total": job.chapters_total,
        "chapters_done": job.chapters_done,
        "error_message": job.error_message,
        "output_path": job.output_path,
        "queue_position": job.queue_position,
        "duration_seconds": job.duration_seconds,
        "file_size_bytes": job.file_size_bytes,
        "created_at": str(job.created_at) if job.created_at else None,
        "started_at": str(job.started_at) if job.started_at else None,
        "completed_at": str(job.completed_at) if job.completed_at else None,
    }
