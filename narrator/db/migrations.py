import logging
import sqlite3

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    calibre_book_id INTEGER UNIQUE NOT NULL,
    title           TEXT NOT NULL,
    author          TEXT NOT NULL,
    series          TEXT,
    series_index    REAL,
    voice           TEXT NOT NULL DEFAULT 'af_heart',
    status          TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','extracting','synthesizing','building','complete','failed')),
    chapters_total  INTEGER DEFAULT 0,
    chapters_done   INTEGER DEFAULT 0,
    error_message   TEXT,
    epub_path       TEXT,
    output_path     TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at      TIMESTAMP,
    completed_at    TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);
"""

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS jobs_new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    calibre_book_id INTEGER NOT NULL,
    title           TEXT NOT NULL,
    author          TEXT NOT NULL,
    series          TEXT,
    series_index    REAL,
    voice           TEXT NOT NULL DEFAULT 'af_heart',
    status          TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','extracting','synthesizing','building','complete','failed')),
    chapters_total  INTEGER DEFAULT 0,
    chapters_done   INTEGER DEFAULT 0,
    error_message   TEXT,
    epub_path       TEXT,
    output_path     TEXT,
    queue_position  INTEGER,
    duration_seconds INTEGER,
    file_size_bytes INTEGER,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at      TIMESTAMP,
    completed_at    TIMESTAMP
);

INSERT INTO jobs_new (
    id, calibre_book_id, title, author, series, series_index, voice, status,
    chapters_total, chapters_done, error_message, epub_path, output_path,
    created_at, started_at, completed_at
)
SELECT
    id, calibre_book_id, title, author, series, series_index, voice, status,
    chapters_total, chapters_done, error_message, epub_path, output_path,
    created_at, started_at, completed_at
FROM jobs;

DROP TABLE jobs;
ALTER TABLE jobs_new RENAME TO jobs;
"""


def run_migrations(conn: sqlite3.Connection):
    current = _get_version(conn)
    if current >= SCHEMA_VERSION:
        return

    if current < 1:
        log.info("Initializing database schema v1")
        conn.executescript(SCHEMA_V1)
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (1,))
        conn.commit()
        current = 1

    if current < 2:
        log.info("Migrating database to v2")
        conn.executescript(SCHEMA_V2)
        conn.execute("UPDATE schema_version SET version = ?", (2,))
        conn.commit()


def _get_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        return row[0] if row else 0
    except sqlite3.OperationalError:
        return 0
