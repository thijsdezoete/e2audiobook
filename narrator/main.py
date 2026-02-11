import logging
import tempfile
from pathlib import Path

import click
import nltk

from narrator.config import Config
from narrator.core.calibre_reader import get_reader
from narrator.core.epub_extractor import extract
from narrator.core.m4b_builder import M4BBuilder
from narrator.core.output_manager import OutputManager
from narrator.core.tts_client import TTSClient
from narrator.db.database import Database
from narrator.db.models import JobStatus
from narrator.job_queue import JobQueue


def _setup_logging(level: str):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _make_config(**overrides) -> Config:
    fields = {}
    for key, val in overrides.items():
        if val is not None:
            fields[key] = val
    return Config(**fields)


@click.group()
@click.option("--log-level", default=None, help="Log level (debug, info, warning, error)")
@click.pass_context
def cli(ctx, log_level):
    config = _make_config()
    if log_level:
        _setup_logging(log_level)
    else:
        _setup_logging(config.log_level)
    ctx.ensure_object(dict)
    ctx.obj["config"] = config


@cli.command()
@click.option("--search", default=None, help="Search by title or author")
@click.pass_context
def list(ctx, search):
    config = ctx.obj["config"]
    reader = get_reader(config)
    output_mgr = OutputManager(config)

    books = reader.search(search) if search else reader.list_books()

    if not books:
        click.echo("No books found.")
        return

    db = Database(config)
    db.connect()
    queue = JobQueue(db)
    jobs = {j.calibre_book_id: j for j in queue.list_jobs()}

    click.echo(f"{'ID':>5}  {'Status':>12}  {'Title':<40}  {'Author':<25}  {'Series'}")
    click.echo("-" * 110)
    for book in books:
        job = jobs.get(book.id)
        if job:
            status = job.status.value
        elif output_mgr.already_exists({"author": book.author, "title": book.title}, series=book.series):
            status = "converted"
        else:
            status = ""
        series_str = f"{book.series} #{book.series_index}" if book.series else ""
        click.echo(f"{book.id:>5}  {status:>12}  {book.title:<40.40}  {book.author:<25.25}  {series_str}")

    db.close()


@cli.command()
@click.argument("book_id", type=int)
@click.option("--voice", default=None, help="TTS voice ID")
@click.pass_context
def convert(ctx, book_id, voice):
    config = ctx.obj["config"]
    voice = voice or config.default_voice
    nltk.download("punkt_tab", quiet=True)

    reader = get_reader(config)
    book = reader.get_book(book_id)
    epub_path = reader.get_epub_path(book)
    is_kepub = book.format_name == "KEPUB"

    db = Database(config)
    db.connect()
    queue = JobQueue(db)

    if queue.is_duplicate(book.id):
        click.echo(f"Book already has a job: {book.title}")
        db.close()
        return

    job = queue.enqueue(
        calibre_book_id=book.id,
        title=book.title,
        author=book.author,
        voice=voice,
        epub_path=str(epub_path),
        series=book.series,
        series_index=book.series_index,
    )

    try:
        queue.start_job(job.id, JobStatus.EXTRACTING)
        click.echo(f"Parsing: {epub_path.name}")
        extracted = extract(str(epub_path), is_kepub=is_kepub)

        queue.start_job(job.id, JobStatus.SYNTHESIZING, chapters_total=len(extracted.chapters))

        tts = TTSClient(config)
        tts.wait_until_ready()

        click.echo(f"Synthesizing {len(extracted.chapters)} chapters with voice '{voice}'...")

        with tempfile.TemporaryDirectory() as tmp_dir:
            wav_paths = []
            for ch_idx, chapter in enumerate(extracted.chapters, 1):
                wav_path = Path(tmp_dir) / f"chapter_{ch_idx:03d}.wav"
                tts.synthesize_chapter(
                    title=chapter.title,
                    text=chapter.text,
                    voice=voice,
                    output_path=wav_path,
                    chapter_num=ch_idx,
                    total_chapters=len(extracted.chapters),
                )
                wav_paths.append((chapter.title, str(wav_path)))
                queue.update_progress(job.id, JobStatus.SYNTHESIZING, ch_idx)

            queue.update_progress(job.id, JobStatus.BUILDING, len(extracted.chapters))

            click.echo("Building M4B...")
            metadata = {
                "title": extracted.metadata.title,
                "author": extracted.metadata.author,
                "date": extracted.metadata.date,
                "description": extracted.metadata.description,
            }
            builder = M4BBuilder(config)
            m4b_path = builder.build(wav_paths, metadata, extracted.cover_image, tmp_dir)
            builder.validate(m4b_path, len(extracted.chapters))

            click.echo("Writing output...")
            output_mgr = OutputManager(config)
            book_dir = output_mgr.write(
                m4b_path, metadata, extracted.cover_image,
                voice=voice, series=book.series, series_index=book.series_index,
            )

        queue.complete_job(job.id, book_dir)

        click.echo("\nConversion complete.")
        click.echo(f"  Title:    {extracted.metadata.title}")
        click.echo(f"  Author:   {extracted.metadata.author}")
        click.echo(f"  Chapters: {len(extracted.chapters)}")
        click.echo(f"  Output:   {book_dir}")

    except Exception as e:
        queue.fail_job(job.id, str(e))
        click.echo(f"Conversion failed: {e}", err=True)
        raise click.Abort from e
    finally:
        db.close()


@cli.command("sync-all")
@click.option("--voice", default=None, help="TTS voice ID")
@click.pass_context
def sync_all(ctx, voice):
    config = ctx.obj["config"]
    voice = voice or config.default_voice

    reader = get_reader(config)
    output_mgr = OutputManager(config)
    db = Database(config)
    db.connect()
    queue = JobQueue(db)

    books = reader.list_books()
    queued = 0
    for book in books:
        if queue.is_duplicate(book.id):
            continue
        metadata = {"author": book.author, "title": book.title}
        if output_mgr.already_exists(metadata, series=book.series):
            continue
        epub_path = reader.get_epub_path(book)
        queue.enqueue(
            calibre_book_id=book.id,
            title=book.title,
            author=book.author,
            voice=voice,
            epub_path=str(epub_path),
            series=book.series,
            series_index=book.series_index,
        )
        queued += 1

    click.echo(f"Queued {queued} books for conversion.")
    db.close()


@cli.command()
@click.pass_context
def status(ctx):
    config = ctx.obj["config"]
    db = Database(config)
    db.connect()
    queue = JobQueue(db)

    for label, st in [
        ("Active", JobStatus.SYNTHESIZING),
        ("Pending", JobStatus.PENDING),
        ("Complete", JobStatus.COMPLETE),
        ("Failed", JobStatus.FAILED),
    ]:
        jobs = queue.list_jobs(status=st)
        if not jobs:
            continue
        click.echo(f"\n{label} ({len(jobs)}):")
        for job in jobs:
            progress = ""
            if st == JobStatus.SYNTHESIZING:
                progress = f" [{job.chapters_done}/{job.chapters_total}]"
            error = ""
            if st == JobStatus.FAILED and job.error_message:
                error = f" -- {job.error_message}"
            click.echo(f"  #{job.id} {job.title} by {job.author}{progress}{error}")

    db.close()
