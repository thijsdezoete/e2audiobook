import logging
import sqlite3

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

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


def run_migrations(conn: sqlite3.Connection):
    current = _get_version(conn)
    if current >= SCHEMA_VERSION:
        return

    if current < 1:
        log.info("Initializing database schema v%d", SCHEMA_VERSION)
        conn.executescript(SCHEMA_V1)
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()


def _get_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        return row[0] if row else 0
    except sqlite3.OperationalError:
        return 0
