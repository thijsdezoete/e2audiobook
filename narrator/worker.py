import asyncio
import logging
import tempfile
from datetime import datetime
from pathlib import Path

import nltk

from narrator.config import Settings
from narrator.core.epub_extractor import extract
from narrator.core.m4b_builder import M4BBuilder
from narrator.core.output_manager import OutputManager
from narrator.core.tts_client import TTSClient
from narrator.db.database import Database
from narrator.db.models import JobStatus
from narrator.health import state as health_state
from narrator.job_queue import JobQueue

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.queue = JobQueue(db)
        self._running = False
        self._current_job_id: int | None = None
        self._event_callback = None

    @property
    def current_job_id(self) -> int | None:
        return self._current_job_id

    def set_event_callback(self, callback):
        self._event_callback = callback

    def _publish(self, event_type: str, data: dict):
        if self._event_callback:
            try:
                self._event_callback(event_type, data)
            except Exception:
                log.exception("Event callback error")

    async def run(self):
        self._running = True
        health_state.worker_running = True
        log.info("Worker started")

        await self._resume_interrupted()

        while self._running:
            if health_state.queue_paused:
                await asyncio.sleep(5)
                continue

            if self._in_quiet_hours():
                await asyncio.sleep(60)
                continue

            job = await asyncio.to_thread(self.queue.next_pending)
            if not job:
                await asyncio.sleep(5)
                continue

            delay = int(self.settings.get("delay_between_books"))
            if delay > 0:
                await asyncio.sleep(delay)

            await self._process_job(job.id)

        health_state.worker_running = False
        log.info("Worker stopped")

    def stop(self):
        self._running = False

    async def _resume_interrupted(self):
        jobs = await asyncio.to_thread(self.queue.get_resumable_jobs)
        for job in jobs:
            log.info("Resuming interrupted job #%d: %s", job.id, job.title)
            await asyncio.to_thread(
                self.queue.update_progress, job.id, JobStatus.PENDING, 0
            )

    def _in_quiet_hours(self) -> bool:
        start = self.settings.get("quiet_hours_start")
        end = self.settings.get("quiet_hours_end")
        if not start or not end:
            return False
        try:
            now = datetime.now().strftime("%H:%M")
            if start <= end:
                return start <= now <= end
            return now >= start or now <= end
        except Exception:
            return False

    async def _process_job(self, job_id: int):
        self._current_job_id = job_id
        try:
            job = await asyncio.to_thread(self.queue.get_job, job_id)
            self._publish("job_started", {"job_id": job.id, "title": job.title, "author": job.author})
            log.info("Processing job #%d: %s by %s", job.id, job.title, job.author)

            await asyncio.to_thread(self.queue.start_job, job.id, JobStatus.EXTRACTING)

            epub_path = Path(job.epub_path)
            is_kepub = epub_path.name.lower().endswith((".kepub.epub", ".kepub"))
            extracted = await asyncio.to_thread(extract, str(epub_path), is_kepub)

            chapter_count = len(extracted.chapters)
            await asyncio.to_thread(
                self.queue.start_job, job.id, JobStatus.SYNTHESIZING, chapter_count
            )

            await asyncio.to_thread(nltk.download, "punkt_tab", quiet=True)
            tts = TTSClient(self.settings)
            await asyncio.to_thread(tts.wait_until_ready)

            with tempfile.TemporaryDirectory() as tmp_dir:
                wav_paths = []
                for ch_idx, chapter in enumerate(extracted.chapters, 1):
                    if not self._running:
                        log.info("Worker stopping, pausing job #%d", job.id)
                        return

                    if health_state.queue_paused:
                        log.info("Queue paused, stopping after current chapter")
                        await asyncio.to_thread(
                            self.queue.update_progress, job.id, JobStatus.PENDING, ch_idx - 1
                        )
                        return

                    wav_path = Path(tmp_dir) / f"chapter_{ch_idx:03d}.wav"
                    self._publish("chapter_started", {
                        "job_id": job.id, "chapter": ch_idx, "total": chapter_count, "title": chapter.title,
                    })

                    await asyncio.to_thread(
                        tts.synthesize_chapter,
                        title=chapter.title,
                        text=chapter.text,
                        voice=job.voice,
                        output_path=wav_path,
                        chapter_num=ch_idx,
                        total_chapters=chapter_count,
                    )
                    wav_paths.append((chapter.title, str(wav_path)))
                    await asyncio.to_thread(
                        self.queue.update_progress, job.id, JobStatus.SYNTHESIZING, ch_idx
                    )
                    self._publish("chapter_completed", {
                        "job_id": job.id, "chapter": ch_idx, "total": chapter_count,
                    })

                await asyncio.to_thread(
                    self.queue.update_progress, job.id, JobStatus.BUILDING, chapter_count
                )

                metadata = {
                    "title": extracted.metadata.title,
                    "author": extracted.metadata.author,
                    "date": extracted.metadata.date,
                    "description": extracted.metadata.description,
                }
                builder = M4BBuilder(self.settings)
                m4b_path = await asyncio.to_thread(
                    builder.build, wav_paths, metadata, extracted.cover_image, tmp_dir
                )
                validation = await asyncio.to_thread(
                    builder.validate, m4b_path, chapter_count
                )

                output_mgr = OutputManager(self.settings)
                book_dir = await asyncio.to_thread(
                    output_mgr.write, m4b_path, metadata, extracted.cover_image,
                    job.voice, job.series, job.series_index,
                )

            await asyncio.to_thread(
                self.queue.complete_job, job.id, book_dir,
                validation.duration_ms // 1000 if validation else None,
                validation.size_bytes if validation else None,
            )
            self._publish("job_completed", {
                "job_id": job.id, "title": job.title, "output_path": book_dir,
            })
            log.info("Job #%d complete: %s", job.id, book_dir)

        except Exception as e:
            log.exception("Job #%d failed", job_id)
            try:
                await asyncio.to_thread(self.queue.fail_job, job_id, str(e))
            except Exception:
                log.exception("Failed to mark job #%d as failed", job_id)
            self._publish("job_failed", {"job_id": job_id, "error": str(e)})
        finally:
            self._current_job_id = None
