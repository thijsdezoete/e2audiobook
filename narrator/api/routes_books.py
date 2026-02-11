import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()
log = logging.getLogger(__name__)


@router.get("")
async def list_books(
    search: str = Query(default=""),
    author: str = Query(default=""),
    sort: str = Query(default="title"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=1, le=100),
):
    from narrator.app import db, settings
    from narrator.core.calibre_reader import get_reader
    from narrator.core.output_manager import OutputManager
    from narrator.job_queue import JobQueue

    reader = get_reader(settings)
    books = await asyncio.to_thread(reader.list_books)

    if search:
        sl = search.lower()
        books = [b for b in books if sl in b.title.lower() or sl in b.author.lower()]
    if author:
        books = [b for b in books if b.author == author]

    if sort == "author":
        books.sort(key=lambda b: (b.author.lower(), b.title.lower()))
    else:
        books.sort(key=lambda b: b.title.lower())

    total = len(books)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    books_page = books[(page - 1) * per_page: page * per_page]

    queue = JobQueue(db)
    jobs = await asyncio.to_thread(queue.list_jobs)
    job_map = {j.calibre_book_id: j for j in jobs}
    output_mgr = OutputManager(settings)

    results = []
    for b in books_page:
        job = job_map.get(b.id)
        if job:
            status = job.status.value
        elif output_mgr.already_exists({"author": b.author, "title": b.title}, series=b.series):
            status = "converted"
        else:
            status = ""
        results.append({
            "id": b.id,
            "title": b.title,
            "author": b.author,
            "series": b.series,
            "series_index": b.series_index,
            "has_cover": b.has_cover,
            "status": status,
        })

    return {
        "books": results,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    }


@router.get("/{book_id}")
async def get_book(book_id: int):
    from narrator.app import settings
    from narrator.core.calibre_reader import get_reader
    from narrator.core.epub_extractor import extract

    reader = get_reader(settings)
    try:
        book = await asyncio.to_thread(reader.get_book, book_id)
    except (FileNotFoundError, Exception) as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    epub_path = await asyncio.to_thread(reader.get_epub_path, book)
    is_kepub = book.format_name == "KEPUB"

    chapters = []
    try:
        extracted = await asyncio.to_thread(extract, str(epub_path), is_kepub)
        chapters = [
            {"title": ch.title, "word_count": ch.word_count, "preview": ch.text[:200]}
            for ch in extracted.chapters
        ]
    except Exception as e:
        log.warning("Chapter extraction failed for book %d: %s", book_id, e)

    return {
        "id": book.id,
        "title": book.title,
        "author": book.author,
        "series": book.series,
        "series_index": book.series_index,
        "description": book.description,
        "has_cover": book.has_cover,
        "chapters": chapters,
    }


@router.post("/{book_id}/convert")
async def convert_book(book_id: int, body: dict | None = None):
    from narrator.app import db, settings
    from narrator.core.calibre_reader import get_reader
    from narrator.job_queue import JobQueue

    voice = (body or {}).get("voice", settings.get("default_voice"))
    reader = get_reader(settings)

    try:
        book = await asyncio.to_thread(reader.get_book, book_id)
    except (FileNotFoundError, Exception) as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    epub_path = await asyncio.to_thread(reader.get_epub_path, book)
    queue = JobQueue(db)

    if await asyncio.to_thread(queue.is_duplicate, book.id):
        raise HTTPException(status_code=409, detail="Book already has an active job")

    job = await asyncio.to_thread(
        queue.enqueue,
        calibre_book_id=book.id,
        title=book.title,
        author=book.author,
        voice=voice,
        epub_path=str(epub_path),
        series=book.series,
        series_index=book.series_index,
    )
    return {"job_id": job.id, "title": job.title, "status": job.status.value}


@router.post("/convert-all")
async def convert_all(body: dict | None = None):
    from narrator.app import db, settings
    from narrator.core.calibre_reader import get_reader
    from narrator.core.output_manager import OutputManager
    from narrator.job_queue import JobQueue

    voice = (body or {}).get("voice", settings.get("default_voice"))
    reader = get_reader(settings)
    output_mgr = OutputManager(settings)
    queue = JobQueue(db)

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

    return {"queued": queued}


@router.post("/convert-batch")
async def convert_batch(body: dict):
    from narrator.app import db, settings
    from narrator.core.calibre_reader import get_reader
    from narrator.job_queue import JobQueue

    book_ids = body.get("book_ids", [])
    voice = body.get("voice", settings.get("default_voice"))
    if not book_ids:
        raise HTTPException(status_code=400, detail="book_ids required")

    reader = get_reader(settings)
    queue = JobQueue(db)
    queued = 0

    for book_id in book_ids:
        try:
            book = await asyncio.to_thread(reader.get_book, book_id)
            if await asyncio.to_thread(queue.is_duplicate, book.id):
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
        except Exception as e:
            log.warning("Failed to queue book %d: %s", book_id, e)

    return {"queued": queued}
