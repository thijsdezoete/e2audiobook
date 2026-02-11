import io
import logging
import shutil
from pathlib import Path

from bs4 import BeautifulSoup
from PIL import Image

from narrator.config import Config, sanitize_filename

log = logging.getLogger(__name__)


class OutputManager:
    def __init__(self, config: Config):
        self.output_dir = Path(config.audiobook_output_path)

    def write(
        self,
        m4b_path: str,
        metadata: dict,
        cover_image: bytes | None,
        voice: str = "af_heart",
        series: str | None = None,
        series_index: float | None = None,
    ) -> str:
        author = sanitize_filename(metadata["author"])
        title = sanitize_filename(metadata["title"])

        if series:
            book_dir = self.output_dir / author / sanitize_filename(series) / title
        else:
            book_dir = self.output_dir / author / title
        book_dir.mkdir(parents=True, exist_ok=True)

        dest_m4b = book_dir / f"{title}.m4b"
        shutil.move(m4b_path, dest_m4b)

        if cover_image:
            img = Image.open(io.BytesIO(cover_image))
            img = img.convert("RGB")
            img.thumbnail((800, 800), Image.LANCZOS)
            img.save(str(book_dir / "cover.jpg"), "JPEG")

        desc_html = metadata.get("description", "")
        if desc_html:
            desc = BeautifulSoup(desc_html, "lxml").get_text(separator="\n").strip()
            (book_dir / "desc.txt").write_text(desc, encoding="utf-8")

        (book_dir / "reader.txt").write_text(
            f"AI Narration ({voice})",
            encoding="utf-8",
        )

        log.info("Output written to %s", book_dir)
        return str(book_dir)

    def already_exists(self, metadata: dict, series: str | None = None) -> bool:
        author = sanitize_filename(metadata["author"])
        title = sanitize_filename(metadata["title"])

        if series:
            book_dir = self.output_dir / author / sanitize_filename(series) / title
        else:
            book_dir = self.output_dir / author / title

        m4b = book_dir / f"{title}.m4b"
        return m4b.exists()
