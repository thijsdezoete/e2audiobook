import logging
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import ebooklib
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from ebooklib import epub

from narrator.config import FRONT_MATTER_SIGNATURES, SKIP_TITLES

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

log = logging.getLogger(__name__)

MIN_CHAPTER_WORDS = 50
FALLBACK_CHAPTER_WORDS = 5000


class EpubExtractionError(Exception):
    pass


@dataclass
class BookMetadata:
    title: str
    author: str
    language: str = "en"
    publisher: str = ""
    date: str = ""
    description: str = ""


@dataclass
class Chapter:
    title: str
    text: str
    word_count: int = 0

    def __post_init__(self):
        if not self.word_count:
            self.word_count = len(self.text.split())


@dataclass
class ExtractedBook:
    metadata: BookMetadata
    chapters: list[Chapter] = field(default_factory=list)
    cover_image: bytes | None = None


def extract(path: str | Path, is_kepub: bool | None = None) -> ExtractedBook:
    path = Path(path)
    if not path.exists():
        raise EpubExtractionError(f"File not found: {path}")

    if is_kepub is None:
        name_lower = str(path).lower()
        is_kepub = name_lower.endswith(".kepub.epub") or name_lower.endswith(".kepub")

    try:
        book = epub.read_epub(str(path), options={"ignore_ncx": False})
    except Exception as e:
        raise EpubExtractionError(f"Failed to parse EPUB: {e}") from e

    metadata = BookMetadata(
        title=_first_meta(book, "title") or path.stem,
        author=_first_meta(book, "creator") or "Unknown Author",
        language=_first_meta(book, "language") or "en",
        publisher=_first_meta(book, "publisher") or "",
        date=_first_meta(book, "date") or "",
        description=_first_meta(book, "description") or "",
    )

    cover_image = _find_external_cover(path) or _extract_cover(book)
    spine_items = _get_spine_items(book)

    chapters = _detect_chapters_toc(book, spine_items, is_kepub)
    if not chapters:
        chapters = _detect_chapters_headings(spine_items, is_kepub)
    if not chapters:
        chapters = _detect_chapters_regex(spine_items, is_kepub)
    if not chapters:
        chapters = _detect_chapters_fixed(spine_items, is_kepub)

    filtered = []
    for title, text in chapters:
        text = _strip_title_from_text(title, text)
        word_count = len(text.split())
        if word_count < MIN_CHAPTER_WORDS:
            continue
        if _is_skippable(title, text):
            log.info("Skipping: %s", title)
            continue
        filtered.append(Chapter(title=title, text=text, word_count=word_count))

    if not filtered:
        raise EpubExtractionError("No chapters found with sufficient text")

    log.info("Found %d chapters:", len(filtered))
    for i, ch in enumerate(filtered, 1):
        log.info("  %d. %s (%d words)", i, ch.title, ch.word_count)

    return ExtractedBook(metadata=metadata, chapters=filtered, cover_image=cover_image)


def _first_meta(book, field_name):
    values = book.get_metadata("DC", field_name)
    if values:
        val = values[0][0]
        if isinstance(val, str):
            return val.strip()
    return None


def _find_external_cover(epub_path):
    epub_dir = Path(epub_path).parent
    for name in ["cover.jpg", "cover.jpeg", "cover.png"]:
        cover_path = epub_dir / name
        if cover_path.exists():
            log.info("Using external cover: %s", cover_path)
            return cover_path.read_bytes()
    return None


def _extract_cover(book):
    cover_id = None
    for _meta_name, meta_content in book.get_metadata("OPF", "cover"):
        cover_id = meta_content.get("content")
        break

    if cover_id:
        for item in book.get_items():
            if item.get_id() == cover_id:
                return item.get_content()

    for item in book.get_items():
        name = item.get_name().lower()
        if "cover" in name and _is_image_content(item):
            return item.get_content()

    for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
        return item.get_content()

    return None


def _is_image_content(item):
    name = item.get_name().lower()
    return name.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"))


def _get_spine_items(book):
    spine_ids = [item_id for item_id, _ in book.spine]
    items = []
    for item_id in spine_ids:
        item = book.get_item_with_id(item_id)
        if item and item.get_type() == ebooklib.ITEM_DOCUMENT:
            items.append(item)
    return items


def _html_to_text(html_content, is_kepub=False):
    soup = BeautifulSoup(html_content, "lxml")
    if is_kepub:
        for span in soup.find_all("span", class_="koboSpan"):
            span.unwrap()
    for el in soup.find_all(
        class_=re.compile(r"(dropcap|drop.?cap|initial|first.?letter|big.?letter)", re.IGNORECASE)
    ):
        el.unwrap()
    text = soup.get_text(separator="\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"(?m)^([A-Z])\n([a-z])", r"\1\2", text)
    return text.strip()


def _detect_chapters_toc(book, spine_items, is_kepub):
    toc = book.toc
    if not toc:
        return []

    toc_entries = _flatten_toc(toc)
    if not toc_entries:
        return []

    item_map = {}
    for item in spine_items:
        name = item.get_name()
        item_map[name] = item
        item_map[name.split("/")[-1]] = item

    soup_cache = {}
    entries_by_file = {}
    for i, (title, href) in enumerate(toc_entries):
        raw_base = href.split("#")[0] if href else ""
        base_href = raw_base.split("/")[-1]
        fragment = href.split("#")[1] if href and "#" in href else None
        if base_href not in entries_by_file:
            entries_by_file[base_href] = []
        entries_by_file[base_href].append((i, title, fragment))

    chapters = [None] * len(toc_entries)
    for base_href, entries in entries_by_file.items():
        item = item_map.get(base_href)
        if not item:
            continue

        has_fragments = any(f is not None for _, _, f in entries)

        if not has_fragments or len(entries) == 1:
            text = _html_to_text(item.get_content(), is_kepub)
            if text:
                for idx, title, _ in entries:
                    chapters[idx] = (title, text)
            continue

        if base_href not in soup_cache:
            soup = BeautifulSoup(item.get_content(), "lxml")
            if is_kepub:
                for span in soup.find_all("span", class_="koboSpan"):
                    span.unwrap()
            soup_cache[base_href] = soup
        soup = soup_cache[base_href]

        anchor_elements = []
        for idx, title, fragment in entries:
            if fragment:
                el = soup.find(id=fragment)
                anchor_elements.append((idx, title, el))
            else:
                anchor_elements.append((idx, title, None))

        for pos, (idx, title, el) in enumerate(anchor_elements):
            if el is None:
                text = _html_to_text(item.get_content(), is_kepub)
            else:
                next_el = None
                for future_pos in range(pos + 1, len(anchor_elements)):
                    if anchor_elements[future_pos][2] is not None:
                        next_el = anchor_elements[future_pos][2]
                        break

                parts = []
                current = el
                while current:
                    if current == next_el:
                        break
                    if hasattr(current, "get_text"):
                        parts.append(current.get_text(separator="\n"))
                    elif isinstance(current, str):
                        parts.append(current.strip())
                    current = current.next_sibling

                if not parts and el.parent:
                    current = el
                    while current:
                        if current == next_el:
                            break
                        if hasattr(current, "get_text"):
                            parts.append(current.get_text(separator="\n"))
                        current = current.find_next()
                        if next_el and current == next_el:
                            break

                text = "\n".join(parts)
                text = re.sub(r"\n{3,}", "\n\n", text).strip()

            if text:
                chapters[idx] = (title, text)

    return [c for c in chapters if c is not None]


def _flatten_toc(toc):
    entries = []
    for entry in toc:
        if isinstance(entry, tuple):
            section, children = entry
            if hasattr(section, "title") and hasattr(section, "href"):
                entries.append((section.title, section.href))
            entries.extend(_flatten_toc(children))
        elif hasattr(entry, "title") and hasattr(entry, "href"):
            entries.append((entry.title, entry.href))
    return entries


def _detect_chapters_headings(spine_items, is_kepub):
    chapters = []
    for item in spine_items:
        soup = BeautifulSoup(item.get_content(), "lxml")
        if is_kepub:
            for span in soup.find_all("span", class_="koboSpan"):
                span.unwrap()

        headings = soup.find_all(["h1", "h2"])
        if not headings:
            text = soup.get_text(separator="\n").strip()
            text = re.sub(r"\n{3,}", "\n\n", text)
            if text:
                chapters.append((f"Section {len(chapters) + 1}", text))
            continue

        parts = []
        for i, heading in enumerate(headings):
            title = heading.get_text(strip=True)
            content_parts = []
            for sibling in heading.find_all_next():
                if sibling in headings[i + 1 :]:
                    break
                if sibling.name not in ["h1", "h2"]:
                    t = sibling.get_text(separator="\n").strip()
                    if t:
                        content_parts.append(t)
            text = "\n\n".join(content_parts)
            text = re.sub(r"\n{3,}", "\n\n", text)
            if title:
                parts.append((title, text))

        chapters.extend(parts)

    return chapters


def _detect_chapters_regex(spine_items, is_kepub):
    full_text = ""
    for item in spine_items:
        full_text += _html_to_text(item.get_content(), is_kepub) + "\n\n"

    pattern = re.compile(
        r"^(Chapter\s+\d+|CHAPTER\s+\d+|Part\s+\w+|PART\s+\w+)",
        re.MULTILINE,
    )
    matches = list(pattern.finditer(full_text))
    if not matches:
        return []

    chapters = []
    for i, match in enumerate(matches):
        title = match.group(0).strip()
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)
        text = full_text[start:end].strip()
        lines = text.split("\n", 1)
        if len(lines) > 1:
            text = lines[1].strip()
        chapters.append((title, text))

    return chapters


def _detect_chapters_fixed(spine_items, is_kepub):
    full_text = ""
    for item in spine_items:
        full_text += _html_to_text(item.get_content(), is_kepub) + "\n\n"

    words = full_text.split()
    if not words:
        return []

    chapters = []
    paragraphs = full_text.split("\n\n")
    current_chunk = []
    current_word_count = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        para_words = len(para.split())
        if current_word_count + para_words > FALLBACK_CHAPTER_WORDS and current_chunk:
            chapters.append((f"Part {len(chapters) + 1}", "\n\n".join(current_chunk)))
            current_chunk = []
            current_word_count = 0
        current_chunk.append(para)
        current_word_count += para_words

    if current_chunk:
        chapters.append((f"Part {len(chapters) + 1}", "\n\n".join(current_chunk)))

    return chapters


def _strip_title_from_text(title, text):
    title_words = re.findall(r"\w+", title.lower())
    if not title_words:
        return text
    search_region = text[: len(title) * 3]
    text_matches = list(re.finditer(r"\w+", search_region, re.IGNORECASE))
    if len(text_matches) >= len(title_words) and all(
        text_matches[i].group().lower() == title_words[i] for i in range(len(title_words))
    ):
        end_pos = text_matches[len(title_words) - 1].end()
        return text[end_pos:].strip()
    return text


def _is_skippable(title, text):
    if SKIP_TITLES.search(title):
        return True
    if len(text.split()) < 500 and FRONT_MATTER_SIGNATURES.search(text):
        return True
    return bool(_looks_like_toc(text))


def _looks_like_toc(text):
    lines = [line.strip() for line in text.strip().split("\n") if line.strip()]
    if len(lines) < 5:
        return False
    chapter_like = sum(
        1
        for line in lines
        if re.match(
            r"^(chapter|part|section|appendix|introduction|foreword|preface|prologue|epilogue)\b",
            line,
            re.IGNORECASE,
        )
        or re.match(r"^\d+[\.\)]\s", line)
    )
    return chapter_like >= 4 and chapter_like / len(lines) > 0.3
