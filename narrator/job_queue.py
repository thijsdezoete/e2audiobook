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
        row = conn.execute(
            "SELECT COALESCE(MAX(COALESCE(queue_position, id)), 0) FROM jobs WHERE status = 'pending'"
        ).fetchone()
        max_pos = row[0]
        conn.execute(
            """INSERT INTO jobs (calibre_book_id, title, author, voice, epub_path, series, series_index, queue_position)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (calibre_book_id, title, author, voice, epub_path, series, series_index, max_pos + 1),
        )
        conn.commit()
        job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        log.info("Enqueued job #%d: %s by %s", job_id, title, author)
        return self.get_job(job_id)

    def is_duplicate(self, calibre_book_id: int) -> bool:
        row = self.db.conn.execute(
            "SELECT id FROM jobs WHERE calibre_book_id = ? AND status NOT IN ('failed')",
            (calibre_book_id,),
        ).fetchone()
        return row is not None

    def next_pending(self) -> Job | None:
        row = self.db.conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY COALESCE(queue_position, id) LIMIT 1",
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

    def complete_job(
        self, job_id: int, output_path: str,
        duration_seconds: int | None = None, file_size_bytes: int | None = None,
    ):
        now = datetime.now(UTC).isoformat()
        self.db.conn.execute(
            """UPDATE jobs SET status = ?, output_path = ?, completed_at = ?,
               duration_seconds = ?, file_size_bytes = ? WHERE id = ?""",
            (JobStatus.COMPLETE, output_path, now, duration_seconds, file_size_bytes, job_id),
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

    def cancel_job(self, job_id: int):
        job = self.get_job(job_id)
        if job.status in (JobStatus.COMPLETE, JobStatus.FAILED):
            return
        self.db.conn.execute(
            "UPDATE jobs SET status = ?, error_message = ?, completed_at = ? WHERE id = ?",
            (JobStatus.FAILED, "Cancelled by user", datetime.now(UTC).isoformat(), job_id),
        )
        self.db.conn.commit()
        log.info("Job #%d cancelled", job_id)

    def retry_job(self, job_id: int) -> Job:
        job = self.get_job(job_id)
        if job.status != JobStatus.FAILED:
            raise ValueError(f"Job {job_id} is not failed (status: {job.status})")
        max_pos = self.db.conn.execute(
            "SELECT COALESCE(MAX(queue_position), 0) FROM jobs WHERE status = 'pending'"
        ).fetchone()[0]
        self.db.conn.execute(
            """UPDATE jobs SET status = ?, error_message = NULL, started_at = NULL,
               completed_at = NULL, chapters_done = 0, queue_position = ? WHERE id = ?""",
            (JobStatus.PENDING, max_pos + 1, job_id),
        )
        self.db.conn.commit()
        log.info("Job #%d queued for retry", job_id)
        return self.get_job(job_id)

    def reorder(self, job_ids: list[int]):
        conn = self.db.conn
        for position, job_id in enumerate(job_ids, 1):
            conn.execute(
                "UPDATE jobs SET queue_position = ? WHERE id = ? AND status = 'pending'",
                (position, job_id),
            )
        conn.commit()

    def get_resumable_jobs(self) -> list[Job]:
        rows = self.db.conn.execute(
            "SELECT * FROM jobs WHERE status IN (?, ?, ?) ORDER BY id",
            (JobStatus.EXTRACTING, JobStatus.SYNTHESIZING, JobStatus.BUILDING),
        ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def list_jobs(self, status: JobStatus | None = None, limit: int = 0, offset: int = 0) -> list[Job]:
        query = "SELECT * FROM jobs"
        params: list = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY id DESC"
        if limit:
            query += " LIMIT ?"
            params.append(limit)
            if offset:
                query += " OFFSET ?"
                params.append(offset)
        rows = self.db.conn.execute(query, params).fetchall()
        return [self._row_to_job(row) for row in rows]

    def count_jobs(self, status: JobStatus | None = None) -> int:
        if status:
            row = self.db.conn.execute("SELECT COUNT(*) FROM jobs WHERE status = ?", (status,)).fetchone()
        else:
            row = self.db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()
        return row[0]

    def queue_summary(self) -> dict:
        rows = self.db.conn.execute(
            "SELECT status, COUNT(*) as count FROM jobs GROUP BY status"
        ).fetchall()
        summary = {s.value: 0 for s in JobStatus}
        for row in rows:
            summary[row["status"]] = row["count"]
        return summary

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
            queue_position=row["queue_position"],
            duration_seconds=row["duration_seconds"],
            file_size_bytes=row["file_size_bytes"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )
