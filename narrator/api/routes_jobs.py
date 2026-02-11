import asyncio

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()


@router.get("")
async def list_jobs(
    status: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=1, le=100),
):
    from narrator.app import db
    from narrator.db.models import JobStatus
    from narrator.job_queue import JobQueue

    queue = JobQueue(db)
    st = JobStatus(status) if status else None
    total = await asyncio.to_thread(queue.count_jobs, st)
    offset = (page - 1) * per_page
    jobs = await asyncio.to_thread(queue.list_jobs, status=st, limit=per_page, offset=offset)

    return {
        "jobs": [_job_dict(j) for j in jobs],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, (total + per_page - 1) // per_page),
    }


@router.get("/{job_id}")
async def get_job(job_id: int):
    from narrator.app import db
    from narrator.job_queue import JobQueue

    queue = JobQueue(db)
    try:
        job = await asyncio.to_thread(queue.get_job, job_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return _job_dict(job)


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
        "epub_path": job.epub_path,
        "output_path": job.output_path,
        "queue_position": job.queue_position,
        "duration_seconds": job.duration_seconds,
        "file_size_bytes": job.file_size_bytes,
        "created_at": str(job.created_at) if job.created_at else None,
        "started_at": str(job.started_at) if job.started_at else None,
        "completed_at": str(job.completed_at) if job.completed_at else None,
    }
