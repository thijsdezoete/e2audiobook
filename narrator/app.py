import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from narrator.config import Settings
from narrator.db.database import Database
from narrator.health import health_monitor
from narrator.health import state as health_state
from narrator.worker import Worker

log = logging.getLogger(__name__)

settings = Settings()
db = Database(settings)
worker = Worker(settings, db)

WEB_DIR = Path(__file__).parent / "web"
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


def _wire_events():
    from narrator.api.routes_queue import publish_event
    from narrator.notifications import schedule_notification

    def on_event(event_type, data):
        publish_event(event_type, data)
        if event_type in ("job_completed", "job_failed"):
            schedule_notification(settings, event_type, data)

    worker.set_event_callback(on_event)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log.info("Starting Narrator server")

    db.connect()
    settings.bind_db(db.conn)
    log.info("Database initialized")

    library_path = settings.get("calibre_library_path")
    output_path = settings.get("audiobook_output_path")
    if Path(library_path).exists():
        log.info("Library mount OK: %s", library_path)
    else:
        log.warning("Library mount not found: %s", library_path)
    if Path(output_path).exists():
        log.info("Output mount OK: %s", output_path)
    else:
        log.warning("Output mount not found: %s", output_path)

    from narrator.api import api_router
    app.include_router(api_router, prefix="/api")

    _wire_events()

    from narrator.watcher import library_watcher

    health_task = asyncio.create_task(
        health_monitor(
            settings.get("tts_url"),
            settings.get("calibre_library_path"),
            settings.get("audiobook_output_path"),
        )
    )
    worker_task = asyncio.create_task(worker.run())
    watcher_task = asyncio.create_task(library_watcher(settings, db))

    yield

    worker.stop()
    health_task.cancel()
    worker_task.cancel()
    watcher_task.cancel()
    for task in (health_task, worker_task, watcher_task):
        with contextlib.suppress(asyncio.CancelledError):
            await task
    db.close()
    log.info("Narrator server stopped")


app = FastAPI(title="Narrator", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")


def _ctx(request: Request, **kwargs) -> dict:
    return {"request": request, "health": health_state, **kwargs}


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    from narrator.job_queue import JobQueue

    queue = JobQueue(db)
    summary = await asyncio.to_thread(queue.queue_summary)
    active_jobs = await asyncio.to_thread(queue.list_jobs, status="synthesizing", limit=1)
    recent_jobs = await asyncio.to_thread(queue.list_jobs, limit=10)
    active = active_jobs[0] if active_jobs else None
    return templates.TemplateResponse("dashboard.html", _ctx(
        request, summary=summary, active=active, recent=recent_jobs,
    ))


@app.get("/library", response_class=HTMLResponse)
async def library_page(request: Request):
    from narrator.core.calibre_reader import get_reader
    from narrator.core.output_manager import OutputManager
    from narrator.job_queue import JobQueue

    reader = get_reader(settings)
    queue = JobQueue(db)

    search = request.query_params.get("search", "")
    author_filter = request.query_params.get("author", "")
    status_filter = request.query_params.get("status", "")
    sort = request.query_params.get("sort", "title")
    page = int(request.query_params.get("page", 1))
    per_page = int(request.query_params.get("per_page", 25))

    books = await asyncio.to_thread(reader.list_books)

    if search:
        sl = search.lower()
        books = [b for b in books if sl in b.title.lower() or sl in b.author.lower()]
    if author_filter:
        books = [b for b in books if b.author == author_filter]

    all_authors = sorted({b.author for b in await asyncio.to_thread(reader.list_books)})

    jobs = await asyncio.to_thread(queue.list_jobs)
    job_map = {j.calibre_book_id: j for j in jobs}
    output_mgr = OutputManager(settings)

    book_statuses = {}
    for b in books:
        job = job_map.get(b.id)
        if job:
            book_statuses[b.id] = job.status.value
        elif output_mgr.already_exists({"author": b.author, "title": b.title}, series=b.series):
            book_statuses[b.id] = "converted"
        else:
            book_statuses[b.id] = ""

    if status_filter:
        books = [b for b in books if book_statuses.get(b.id, "") == status_filter]

    if sort == "author":
        books.sort(key=lambda b: (b.author.lower(), b.title.lower()))
    elif sort == "status":
        books.sort(key=lambda b: (book_statuses.get(b.id, ""), b.title.lower()))
    else:
        books.sort(key=lambda b: b.title.lower())

    total = len(books)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    books_page = books[(page - 1) * per_page: page * per_page]

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("partials/library_table.html", _ctx(
            request, books=books_page, statuses=book_statuses, page=page,
            total_pages=total_pages, total=total, per_page=per_page,
            search=search, author_filter=author_filter, status_filter=status_filter, sort=sort,
        ))

    return templates.TemplateResponse("library.html", _ctx(
        request, books=books_page, statuses=book_statuses, authors=all_authors,
        page=page, total_pages=total_pages, total=total, per_page=per_page,
        search=search, author_filter=author_filter, status_filter=status_filter, sort=sort,
    ))


@app.get("/book/{book_id}", response_class=HTMLResponse)
async def book_detail(request: Request, book_id: int):
    from narrator.core.calibre_reader import get_reader
    from narrator.core.epub_extractor import extract
    from narrator.job_queue import JobQueue

    reader = get_reader(settings)
    book = await asyncio.to_thread(reader.get_book, book_id)
    epub_path = await asyncio.to_thread(reader.get_epub_path, book)
    is_kepub = book.format_name == "KEPUB"

    chapters = []
    try:
        extracted = await asyncio.to_thread(extract, str(epub_path), is_kepub)
        chapters = extracted.chapters
    except Exception as e:
        log.warning("Failed to extract chapters for book %d: %s", book_id, e)

    queue = JobQueue(db)
    jobs = await asyncio.to_thread(queue.list_jobs)
    book_jobs = [j for j in jobs if j.calibre_book_id == book_id]

    return templates.TemplateResponse("book_detail.html", _ctx(
        request, book=book, chapters=chapters, jobs=book_jobs,
    ))


@app.get("/queue", response_class=HTMLResponse)
async def queue_page(request: Request):
    from narrator.job_queue import JobQueue

    queue = JobQueue(db)

    active_jobs = []
    for status in ("extracting", "synthesizing", "building"):
        active_jobs.extend(await asyncio.to_thread(queue.list_jobs, status=status))
    pending_jobs = await asyncio.to_thread(queue.list_jobs, status="pending")
    completed_jobs = await asyncio.to_thread(queue.list_jobs, status="complete", limit=50)
    failed_jobs = await asyncio.to_thread(queue.list_jobs, status="failed", limit=50)

    return templates.TemplateResponse("queue.html", _ctx(
        request, active=active_jobs, pending=pending_jobs,
        completed=completed_jobs, failed=failed_jobs,
    ))


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    all_settings = settings.get_all()
    return templates.TemplateResponse("settings.html", _ctx(request, settings=all_settings))


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    return templates.TemplateResponse("logs.html", _ctx(request))


@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request):
    from narrator.job_queue import JobQueue

    queue = JobQueue(db)
    summary = await asyncio.to_thread(queue.queue_summary)
    jobs = await asyncio.to_thread(queue.list_jobs, status="complete")

    total_duration = sum(j.duration_seconds or 0 for j in jobs)
    total_size = sum(j.file_size_bytes or 0 for j in jobs)

    return templates.TemplateResponse("stats.html", _ctx(
        request, summary=summary, total_books=len(jobs),
        total_duration=total_duration, total_size=total_size,
        completed_jobs=jobs,
    ))
