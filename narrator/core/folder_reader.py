import logging
from pathlib import Path

from narrator.config import Config
from narrator.core.calibre_reader import Book

log = logging.getLogger(__name__)

EPUB_EXTENSIONS = (".epub", ".kepub.epub", ".kepub")


class FolderReader:
    def __init__(self, config: Config):
        self.library_path = Path(config.calibre_library_path)
        self._books: list[Book] | None = None

    def _scan(self) -> list[Book]:
        if self._books is not None:
            return self._books

        if not self.library_path.exists():
            log.warning("Library path does not exist: %s", self.library_path)
            self._books = []
            return self._books

        epub_files = sorted(
            p for p in self.library_path.rglob("*")
            if p.suffix.lower() in (".epub", ".kepub") or p.name.lower().endswith(".kepub.epub")
        )

        books = []
        for idx, epub_path in enumerate(epub_files, 1):
            name_lower = epub_path.name.lower()
            format_name = "KEPUB" if name_lower.endswith((".kepub.epub", ".kepub")) else "EPUB"

            title = epub_path.stem
            if title.lower().endswith(".kepub"):
                title = title[:-6]

            rel = epub_path.relative_to(self.library_path)
            parent_parts = rel.parent.parts
            author = parent_parts[0] if parent_parts else "Unknown Author"

            cover_path = epub_path.parent / "cover.jpg"

            books.append(Book(
                id=idx,
                title=title,
                author=author,
                path=str(epub_path.parent.relative_to(self.library_path)),
                format_name=format_name,
                format_filename=epub_path.stem,
                has_cover=cover_path.exists(),
            ))

        log.info("Scanned %d EPUB files from %s", len(books), self.library_path)
        self._books = books
        return self._books

    def list_books(self) -> list[Book]:
        return self._scan()

    def search(self, query: str) -> list[Book]:
        query_lower = query.lower()
        return [
            b for b in self._scan()
            if query_lower in b.title.lower() or query_lower in b.author.lower()
        ]

    def get_book(self, book_id: int) -> Book:
        for book in self._scan():
            if book.id == book_id:
                return book
        raise FileNotFoundError(f"Book with id {book_id} not found")

    def get_epub_path(self, book: Book) -> Path:
        ext = book.format_name.lower()
        if ext == "kepub":
            ext = "kepub.epub"
        path = self.library_path / book.path / f"{book.format_filename}.{ext}"
        if not path.exists():
            raise FileNotFoundError(f"EPUB file not found: {path}")
        return path

    def get_cover_path(self, book: Book) -> Path | None:
        if not book.has_cover:
            return None
        path = self.library_path / book.path / "cover.jpg"
        return path if path.exists() else None
