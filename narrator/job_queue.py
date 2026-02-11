import logging
from datetime import UTC, datetime

from narrator.db.database import Database
from narrator.db.models import Job, JobStatus

log = logging.getLogger(__name__)


class JobQueue:
    def __init__(self, db: Database):
        self.db = db

    def enqueue(
        self,
        calibre_book_id: int,
        title: str,
        author: str,
        voice: str,
        epub_path: str,
        series: str | None = None,
        series_index: float | None = None,
    ) -> Job:
        conn = self.db.conn
        conn.execute(
            """INSERT INTO jobs (calibre_book_id, title, author, voice, epub_path, series, series_index)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (calibre_book_id, title, author, voice, epub_path, series, series_index),
        )
        conn.commit()
        job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        log.info("Enqueued job #%d: %s by %s", job_id, title, author)
        return self.get_job(job_id)

    def is_duplicate(self, calibre_book_id: int) -> bool:
        row = self.db.conn.execute(
            "SELECT id FROM jobs WHERE calibre_book_id = ?", (calibre_book_id,)
        ).fetchone()
        return row is not None

    def next_pending(self) -> Job | None:
        row = self.db.conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY id LIMIT 1",
            (JobStatus.PENDING,),
        ).fetchone()
        return self._row_to_job(row) if row else None

    def get_job(self, job_id: int) -> Job:
        row = self.db.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            raise ValueError(f"Job {job_id} not found")
        return self._row_to_job(row)

    def start_job(self, job_id: int, status: JobStatus, chapters_total: int = 0):
        now = datetime.now(UTC).isoformat()
        self.db.conn.execute(
            "UPDATE jobs SET status = ?, started_at = ?, chapters_total = ? WHERE id = ?",
            (status, now, chapters_total, job_id),
        )
        self.db.conn.commit()

    def update_progress(self, job_id: int, status: JobStatus, chapters_done: int):
        self.db.conn.execute(
            "UPDATE jobs SET status = ?, chapters_done = ? WHERE id = ?",
            (status, chapters_done, job_id),
        )
        self.db.conn.commit()

    def complete_job(self, job_id: int, output_path: str):
        now = datetime.now(UTC).isoformat()
        self.db.conn.execute(
            "UPDATE jobs SET status = ?, output_path = ?, completed_at = ? WHERE id = ?",
            (JobStatus.COMPLETE, output_path, now, job_id),
        )
        self.db.conn.commit()
        log.info("Job #%d completed: %s", job_id, output_path)

    def fail_job(self, job_id: int, error_message: str):
        now = datetime.now(UTC).isoformat()
        self.db.conn.execute(
            "UPDATE jobs SET status = ?, error_message = ?, completed_at = ? WHERE id = ?",
            (JobStatus.FAILED, error_message, now, job_id),
        )
        self.db.conn.commit()
        log.error("Job #%d failed: %s", job_id, error_message)

    def get_resumable_jobs(self) -> list[Job]:
        rows = self.db.conn.execute(
            "SELECT * FROM jobs WHERE status IN (?, ?, ?) ORDER BY id",
            (JobStatus.EXTRACTING, JobStatus.SYNTHESIZING, JobStatus.BUILDING),
        ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def list_jobs(self, status: JobStatus | None = None) -> list[Job]:
        if status:
            rows = self.db.conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY id", (status,)
            ).fetchall()
        else:
            rows = self.db.conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        return [self._row_to_job(row) for row in rows]

    def _row_to_job(self, row) -> Job:
        return Job(
            id=row["id"],
            calibre_book_id=row["calibre_book_id"],
            title=row["title"],
            author=row["author"],
            voice=row["voice"],
            status=JobStatus(row["status"]),
            series=row["series"],
            series_index=row["series_index"],
            chapters_total=row["chapters_total"],
            chapters_done=row["chapters_done"],
            error_message=row["error_message"],
            epub_path=row["epub_path"],
            output_path=row["output_path"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )
