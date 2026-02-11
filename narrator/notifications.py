import asyncio
import logging

import httpx

log = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task] = set()


async def send_webhook(url: str, payload: dict):
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload)
            log.info("Webhook sent to %s: %d", url, resp.status_code)
    except Exception as e:
        log.warning("Webhook failed: %s", e)


async def notify_job_complete(settings, job):
    url = settings.get("webhook_url")
    if not url or settings.get("webhook_on_complete") != "true":
        return
    await send_webhook(url, {
        "event": "job_completed",
        "job_id": job.id,
        "title": job.title,
        "author": job.author,
        "output_path": job.output_path,
        "duration_seconds": job.duration_seconds,
    })


async def notify_job_failed(settings, job):
    url = settings.get("webhook_url")
    if not url or settings.get("webhook_on_failure") != "true":
        return
    await send_webhook(url, {
        "event": "job_failed",
        "job_id": job.id,
        "title": job.title,
        "author": job.author,
        "error": job.error_message,
    })


def schedule_notification(settings, event_type: str, job):
    if event_type == "job_completed":
        task = asyncio.create_task(notify_job_complete(settings, job))
    elif event_type == "job_failed":
        task = asyncio.create_task(notify_job_failed(settings, job))
    else:
        return
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
