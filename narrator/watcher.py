import asyncio
import logging

from narrator.config import Settings
from narrator.core.calibre_reader import get_reader
from narrator.core.output_manager import OutputManager
from narrator.db.database import Database
from narrator.job_queue import JobQueue

log = logging.getLogger(__name__)


async def library_watcher(settings: Settings, db: Database):
    while True:
        try:
            if settings.get("auto_convert") != "true":
                await asyncio.sleep(60)
                continue

            interval = int(settings.get("auto_scan_interval") or 300)
            await asyncio.sleep(interval)

            log.info("Auto-scan: checking for new books")
            reader = get_reader(settings)
            output_mgr = OutputManager(settings)
            queue = JobQueue(db)
            voice = settings.get("default_voice")

            books = await asyncio.to_thread(reader.list_books)
            queued = 0
            for book in books:
                if await asyncio.to_thread(queue.is_duplicate, book.id):
                    continue
                metadata = {"author": book.author, "title": book.title}
                if output_mgr.already_exists(metadata, series=book.series):
                    continue
                epub_path = await asyncio.to_thread(reader.get_epub_path, book)
                await asyncio.to_thread(
                    queue.enqueue,
                    calibre_book_id=book.id,
                    title=book.title,
                    author=book.author,
                    voice=voice,
                    epub_path=str(epub_path),
                    series=book.series,
                    series_index=book.series_index,
                )
                queued += 1

            if queued:
                log.info("Auto-scan: queued %d new books", queued)

        except Exception:
            log.exception("Watcher error")
            await asyncio.sleep(60)
