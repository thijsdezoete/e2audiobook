import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from narrator.config import Config

log = logging.getLogger(__name__)


class CalibreError(Exception):
    pass


@dataclass
class Book:
    id: int
    title: str
    author: str
    series: str | None = None
    series_index: float | None = None
    path: str = ""
    format_name: str = "EPUB"
    format_filename: str = ""
    has_cover: bool = False
    description: str = ""


def _build_book_query(conn: sqlite3.Connection) -> str:
    """Build the book query based on available schema columns and tables."""
    book_cols = {row[1] for row in conn.execute("PRAGMA table_info(books)").fetchall()}
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    series_index_expr = "b.series_index" if "series_index" in book_cols else "NULL AS series_index"
    has_cover_expr = "b.has_cover" if "has_cover" in book_cols else "0 AS has_cover"

    series_join = ""
    series_select = "NULL AS series"
    if "books_series_link" in tables and "series" in tables:
        series_join = "LEFT JOIN books_series_link bsl ON b.id = bsl.book\nLEFT JOIN series s ON bsl.series = s.id"
        series_select = "s.name AS series"

    comments_join = ""
    comments_select = "NULL AS description"
    if "comments" in tables:
        comments_join = "LEFT JOIN comments c ON b.id = c.book"
        comments_select = "c.text AS description"

    return f"""
SELECT
    b.id,
    b.title,
    b.path,
    {has_cover_expr},
    GROUP_CONCAT(DISTINCT a.name) AS author,
    {series_select},
    {series_index_expr},
    d.format AS format_name,
    d.name AS format_filename,
    {comments_select}
FROM books b
LEFT JOIN books_authors_link bal ON b.id = bal.book
LEFT JOIN authors a ON bal.author = a.id
{series_join}
LEFT JOIN data d ON b.id = d.book AND d.format IN ('EPUB', 'KEPUB')
{comments_join}
WHERE d.format IS NOT NULL
GROUP BY b.id
"""


class CalibreReader:
    def __init__(self, config: Config):
        self.library_path = Path(config.calibre_library_path)
        self._db_path = self.library_path / "metadata.db"
        self._query: str | None = None

    def _connect(self) -> sqlite3.Connection:
        if not self._db_path.exists():
            raise CalibreError(f"Calibre metadata.db not found at {self._db_path}")
        uri = f"file:{self._db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        if self._query is None:
            self._query = _build_book_query(conn)
        return conn

    def list_books(self) -> list[Book]:
        conn = self._connect()
        try:
            rows = conn.execute(f"{self._query} ORDER BY b.title").fetchall()
            return [self._row_to_book(row) for row in rows]
        finally:
            conn.close()

    def search(self, query: str) -> list[Book]:
        conn = self._connect()
        try:
            like = f"%{query}%"
            rows = conn.execute(
                f"{self._query} HAVING b.title LIKE ? OR author LIKE ? ORDER BY b.title",
                (like, like),
            ).fetchall()
            return [self._row_to_book(row) for row in rows]
        finally:
            conn.close()

    def get_book(self, book_id: int) -> Book:
        conn = self._connect()
        try:
            row = conn.execute(f"{self._query} HAVING b.id = ?", (book_id,)).fetchone()
            if not row:
                raise CalibreError(f"Book with id {book_id} not found")
            return self._row_to_book(row)
        finally:
            conn.close()

    def get_epub_path(self, book: Book) -> Path:
        ext = book.format_name.lower()
        if ext == "kepub":
            ext = "kepub.epub"
        path = self.library_path / book.path / f"{book.format_filename}.{ext}"
        if not path.exists():
            raise CalibreError(f"EPUB file not found: {path}")
        return path

    def get_cover_path(self, book: Book) -> Path | None:
        if not book.has_cover:
            return None
        path = self.library_path / book.path / "cover.jpg"
        return path if path.exists() else None

    @staticmethod
    def _row_to_book(row: sqlite3.Row) -> Book:
        keys = row.keys()
        return Book(
            id=row["id"],
            title=row["title"],
            author=row["author"] or "Unknown Author",
            series=row["series"] if "series" in keys else None,
            series_index=row["series_index"] if "series_index" in keys else None,
            path=row["path"],
            format_name=row["format_name"],
            format_filename=row["format_filename"],
            has_cover=bool(row["has_cover"]) if "has_cover" in keys else False,
            description=(row["description"] or "") if "description" in keys else "",
        )


def get_reader(config: Config):
    from narrator.core.folder_reader import FolderReader

    db_path = Path(config.calibre_library_path) / "metadata.db"
    if db_path.exists():
        log.info("Calibre library detected at %s", config.calibre_library_path)
        return CalibreReader(config)
    log.info("No metadata.db found, scanning folder for EPUBs at %s", config.calibre_library_path)
    return FolderReader(config)
