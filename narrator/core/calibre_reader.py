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


BOOK_QUERY = """
SELECT
    b.id,
    b.title,
    b.path,
    b.has_cover,
    GROUP_CONCAT(DISTINCT a.name) AS author,
    s.name AS series,
    bsl.series_index,
    d.format AS format_name,
    d.name AS format_filename,
    c.text AS description
FROM books b
LEFT JOIN books_authors_link bal ON b.id = bal.book
LEFT JOIN authors a ON bal.author = a.id
LEFT JOIN books_series_link bsl ON b.id = bsl.book
LEFT JOIN series s ON bsl.series = s.id
LEFT JOIN data d ON b.id = d.book AND d.format IN ('EPUB', 'KEPUB')
LEFT JOIN comments c ON b.id = c.book
WHERE d.format IS NOT NULL
GROUP BY b.id
"""


class CalibreReader:
    def __init__(self, config: Config):
        self.library_path = Path(config.calibre_library_path)
        self._db_path = self.library_path / "metadata.db"

    def _connect(self) -> sqlite3.Connection:
        if not self._db_path.exists():
            raise CalibreError(f"Calibre metadata.db not found at {self._db_path}")
        uri = f"file:{self._db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def list_books(self) -> list[Book]:
        conn = self._connect()
        try:
            rows = conn.execute(f"{BOOK_QUERY} ORDER BY b.title").fetchall()
            return [self._row_to_book(row) for row in rows]
        finally:
            conn.close()

    def search(self, query: str) -> list[Book]:
        conn = self._connect()
        try:
            like = f"%{query}%"
            rows = conn.execute(
                f"{BOOK_QUERY} HAVING b.title LIKE ? OR author LIKE ? ORDER BY b.title",
                (like, like),
            ).fetchall()
            return [self._row_to_book(row) for row in rows]
        finally:
            conn.close()

    def get_book(self, book_id: int) -> Book:
        conn = self._connect()
        try:
            row = conn.execute(f"{BOOK_QUERY} HAVING b.id = ?", (book_id,)).fetchone()
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

    def _row_to_book(self, row: sqlite3.Row) -> Book:
        return Book(
            id=row["id"],
            title=row["title"],
            author=row["author"] or "Unknown Author",
            series=row["series"],
            series_index=row["series_index"],
            path=row["path"],
            format_name=row["format_name"],
            format_filename=row["format_filename"],
            has_cover=bool(row["has_cover"]),
            description=row["description"] or "",
        )


def get_reader(config: Config):
    from narrator.core.folder_reader import FolderReader

    db_path = Path(config.calibre_library_path) / "metadata.db"
    if db_path.exists():
        log.info("Calibre library detected at %s", config.calibre_library_path)
        return CalibreReader(config)
    log.info("No metadata.db found, scanning folder for EPUBs at %s", config.calibre_library_path)
    return FolderReader(config)
