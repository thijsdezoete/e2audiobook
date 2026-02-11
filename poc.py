import argparse
import http.server
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import warnings
from pathlib import Path
from urllib.parse import quote

import ebooklib
import nltk
import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from ebooklib import epub
from PIL import Image
from pydub import AudioSegment

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

TOKEN_LIMIT = 250
TOKEN_FLOOR = 80
CHARS_PER_TOKEN = 3.5
MIN_CHAPTER_WORDS = 50
FALLBACK_CHAPTER_WORDS = 5000
CROSSFADE_MS = 50
AAC_BITRATE = "128k"
MAX_RETRIES = 5
RETRY_BACKOFF = [5, 10, 20, 40, 60]
TTS_STARTUP_TIMEOUT = 300
TTS_COOLDOWN = 1.0
TTS_REST_INTERVAL = 10
TTS_REST_DURATION = 5
SANITIZE_CHARS = re.compile(r'[/\\:*?"<>|]')

SKIP_TITLES = re.compile(
    r"^(copyright|legal|disclaimer|dedication|epigraph|"
    r"acknowledgm|table of contents|contents|title page|"
    r"about the (author|publisher)|also by|other books|"
    r"cover|frontispiece|half.?title|colophon|imprint|"
    r"praise|acclaim|blurb|reviews|"
    r"notes|endnotes|footnotes|index|bibliography|"
    r"references|glossary|further reading|sources)",
    re.IGNORECASE,
)

FRONT_MATTER_SIGNATURES = re.compile(
    r"(all rights reserved|isbn[\s:\-]|"
    r"published by|library of congress|"
    r"cataloging.in.publication|"
    r"printed in (the )?(united states|u\.?s\.?|uk|"
    r"great britain|canada|australia)|"
    r"first (edition|printing|published)|"
    r"no part of this (book|publication)|"
    r"permission .{0,40} (publisher|reproduce)|"
    r"cover (design|art|image|illustration) by)",
    re.IGNORECASE,
)


def sanitize_filename(name):
    return SANITIZE_CHARS.sub("_", name).strip()


def parse_epub(path):
    book = epub.read_epub(path, options={"ignore_ncx": False})

    metadata = {
        "title": _first_meta(book, "title") or Path(path).stem,
        "author": _first_meta(book, "creator") or "Unknown Author",
        "language": _first_meta(book, "language") or "en",
        "publisher": _first_meta(book, "publisher") or "",
        "date": _first_meta(book, "date") or "",
        "description": _first_meta(book, "description") or "",
    }

    cover_image = _find_external_cover(path) or _extract_cover(book)
    spine_items = _get_spine_items(book)
    is_kepub = str(path).lower().endswith(".kepub.epub") or str(path).lower().endswith(".kepub")

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
            print(f"  Skipping: {title}")
            continue
        filtered.append((title, text))
    chapters = filtered

    print(f"Found {len(chapters)} chapters:")
    for i, (title, text) in enumerate(chapters, 1):
        word_count = len(text.split())
        print(f"  {i}. {title} ({word_count} words)")

    return metadata, chapters, cover_image


def _first_meta(book, field):
    values = book.get_metadata("DC", field)
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
            print(f"  Using external cover: {cover_path}")
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
    for el in soup.find_all(class_=re.compile(r"(dropcap|drop.?cap|initial|first.?letter|big.?letter)", re.IGNORECASE)):
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
    if len(text_matches) >= len(title_words):
        if all(text_matches[i].group().lower() == title_words[i] for i in range(len(title_words))):
            end_pos = text_matches[len(title_words) - 1].end()
            return text[end_pos:].strip()
    return text


def _is_skippable(title, text):
    if SKIP_TITLES.search(title):
        return True
    if len(text.split()) < 500 and FRONT_MATTER_SIGNATURES.search(text):
        return True
    if _looks_like_toc(text):
        return True
    return False


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


def chunk_text(text, limit=TOKEN_LIMIT):
    sentences = nltk.sent_tokenize(text)
    chunks = []
    current_chunk = []
    current_tokens = 0

    for sentence in sentences:
        token_est = len(sentence) / CHARS_PER_TOKEN
        if token_est > limit:
            if current_chunk:
                chunks.append(" ".join(current_chunk))
                current_chunk = []
                current_tokens = 0
            chunks.extend(_split_long_sentence(sentence, limit))
            continue

        if current_tokens + token_est > limit and current_tokens >= TOKEN_FLOOR:
            chunks.append(" ".join(current_chunk))
            current_chunk = []
            current_tokens = 0

        current_chunk.append(sentence)
        current_tokens += token_est

    if current_chunk:
        tail = " ".join(current_chunk)
        tail_tokens = len(tail) / CHARS_PER_TOKEN
        if chunks and tail_tokens < TOKEN_FLOOR:
            chunks[-1] = chunks[-1] + " " + tail
        else:
            chunks.append(tail)

    return chunks


def _split_long_sentence(sentence, limit):
    target_chars = int(limit * CHARS_PER_TOKEN * 0.9)
    parts = []

    while len(sentence) / CHARS_PER_TOKEN > limit:
        split_zone = sentence[:target_chars]
        split_at = -1

        for delim in ["; ", ", "]:
            idx = split_zone.rfind(delim)
            if idx > 0:
                split_at = idx + len(delim)
                break

        if split_at < 0:
            idx = split_zone.rfind(" ")
            split_at = idx if idx > 0 else target_chars

        parts.append(sentence[:split_at].strip())
        sentence = sentence[split_at:].strip()

    if sentence:
        parts.append(sentence)

    return parts


def synthesize_chapters(chapters, tts_url, voice, tmp_dir, start_chapter=1):
    wav_paths = []
    total_chapters = len(chapters)

    for ch_idx, (title, text) in enumerate(chapters, 1):
        wav_path = Path(tmp_dir) / f"chapter_{ch_idx:03d}.wav"

        if wav_path.exists():
            print(f"Chapter {ch_idx}/{total_chapters}: {title} [cached]")
            wav_paths.append((title, str(wav_path)))
            continue

        if ch_idx < start_chapter:
            print(f"Chapter {ch_idx}/{total_chapters}: {title} [skipped]")
            continue

        spoken_title = title.title() if title.isupper() else title
        chunks = [f"{spoken_title}."] + chunk_text(text)
        total_chunks = len(chunks)
        segments = []

        for chunk_idx, chunk in enumerate(chunks, 1):
            if chunk_idx > 1 and (chunk_idx - 1) % TTS_REST_INTERVAL == 0:
                print(f"  Resting {TTS_REST_DURATION}s to let TTS recover VRAM...")
                time.sleep(TTS_REST_DURATION)
            print(f"Chapter {ch_idx}/{total_chapters} -- Chunk {chunk_idx}/{total_chunks}: {title}")
            audio_bytes = _tts_request(tts_url, chunk, voice)
            segment = AudioSegment.from_wav(io.BytesIO(audio_bytes))
            segments.append(segment)
            if chunk_idx < total_chunks:
                time.sleep(TTS_COOLDOWN)

        if not segments:
            continue

        combined = segments[0]
        for segment in segments[1:]:
            combined = combined.append(segment, crossfade=CROSSFADE_MS)

        combined.export(str(wav_path), format="wav")
        wav_paths.append((title, str(wav_path)))

    total_duration_ms = sum(
        len(AudioSegment.from_wav(p)) for _, p in wav_paths
    )
    total_seconds = total_duration_ms / 1000
    hours, remainder = divmod(int(total_seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    print(f"Total audio duration: {hours}h {minutes}m {seconds}s")

    return wav_paths


def _wait_for_tts(tts_url):
    print(f"Waiting for TTS server at {tts_url}...")
    deadline = time.monotonic() + TTS_STARTUP_TIMEOUT
    interval = 5
    while time.monotonic() < deadline:
        try:
            resp = requests.get(f"{tts_url}/v1/audio/voices", timeout=10)
            resp.raise_for_status()
            voices = resp.json()
            voice_count = len(voices) if isinstance(voices, list) else "unknown"
            print(f"TTS server responding ({voice_count} voices available)")
            break
        except requests.RequestException:
            remaining = int(deadline - time.monotonic())
            print(f"TTS server not ready, retrying in {interval}s ({remaining}s remaining)...")
            time.sleep(interval)
    else:
        print(f"TTS server not reachable after {TTS_STARTUP_TIMEOUT}s")
        sys.exit(1)

    warmup_text = (
        "This is a warmup request to initialize the text to speech model. "
        "The quick brown fox jumps over the lazy dog near the bank of a quiet river. "
        "She sells seashells by the seashore while the waves crash gently on the sand."
    )
    for attempt in range(3):
        print(f"Warming up TTS model (attempt {attempt + 1}/3)...")
        try:
            warmup = requests.post(
                f"{tts_url}/v1/audio/speech",
                json={"model": "kokoro", "input": warmup_text, "voice": "af_heart", "response_format": "wav"},
                timeout=60,
            )
            warmup.raise_for_status()
            time.sleep(5)
            print("TTS server ready")
            return
        except requests.RequestException as e:
            print(f"Warmup failed ({e}), restarting health check...")
            time.sleep(15)
            deadline = time.monotonic() + TTS_STARTUP_TIMEOUT
            while time.monotonic() < deadline:
                try:
                    resp = requests.get(f"{tts_url}/v1/audio/voices", timeout=10)
                    resp.raise_for_status()
                    break
                except requests.RequestException:
                    time.sleep(5)
    print("TTS server failed to stabilize after warmup attempts")
    sys.exit(1)


def _tts_request(tts_url, text, voice):
    url = f"{tts_url}/v1/audio/speech"
    payload = {
        "model": "kokoro",
        "input": text,
        "voice": voice,
        "response_format": "wav",
    }

    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post(url, json=payload, timeout=120)
            response.raise_for_status()
            return response.content
        except (requests.RequestException, requests.HTTPError) as e:
            if attempt < MAX_RETRIES - 1:
                print(f"TTS request failed (attempt {attempt + 1}/{MAX_RETRIES}): {e}")
                print("Waiting for TTS server to recover...")
                _wait_for_tts(tts_url)
            else:
                print(f"TTS request failed after {MAX_RETRIES} attempts: {e}")
                sys.exit(1)


def build_m4b(wav_paths, metadata, cover_image, tmp_dir, cleanup=True):
    tmp = Path(tmp_dir)
    m4a_paths = []

    for title, wav_path in wav_paths:
        m4a_path = Path(wav_path).with_suffix(".m4a")
        _run_ffmpeg([
            "ffmpeg", "-y", "-i", wav_path,
            "-c:a", "aac", "-b:a", AAC_BITRATE,
            str(m4a_path),
        ])
        if cleanup:
            Path(wav_path).unlink()
        m4a_paths.append((title, str(m4a_path)))

    chapter_durations = []
    for _, m4a_path in m4a_paths:
        duration_ms = _get_duration_ms(m4a_path)
        chapter_durations.append(duration_ms)

    concat_file = tmp / "concat.txt"
    with open(concat_file, "w") as f:
        for _, m4a_path in m4a_paths:
            f.write(f"file '{m4a_path}'\n")

    combined_path = tmp / "combined.m4a"
    _run_ffmpeg([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_file),
        "-c", "copy",
        str(combined_path),
    ])

    if cleanup:
        for _, m4a_path in m4a_paths:
            Path(m4a_path).unlink()
        concat_file.unlink()

    metadata_file = tmp / "ffmetadata.txt"
    with open(metadata_file, "w") as f:
        f.write(";FFMETADATA1\n")
        f.write(f"title={metadata['title']}\n")
        f.write(f"artist={metadata['author']}\n")
        f.write(f"album={metadata['title']}\n")
        f.write("genre=Audiobook\n")
        if metadata.get("date"):
            f.write(f"date={metadata['date']}\n")
        f.write("\n")

        offset = 0
        for i, (title, _) in enumerate(m4a_paths):
            end = offset + chapter_durations[i]
            f.write("[CHAPTER]\n")
            f.write("TIMEBASE=1/1000\n")
            f.write(f"START={offset}\n")
            f.write(f"END={end}\n")
            f.write(f"title={title}\n")
            f.write("\n")
            offset = end

    output_m4b = tmp / f"{sanitize_filename(metadata['title'])}.m4b"

    if cover_image:
        cover_path = tmp / "cover.jpg"
        img = Image.open(io.BytesIO(cover_image))
        img = img.convert("RGB")
        img.save(str(cover_path), "JPEG")

        _run_ffmpeg([
            "ffmpeg", "-y",
            "-i", str(combined_path),
            "-i", str(cover_path),
            "-i", str(metadata_file),
            "-map", "0:a", "-map", "1:v",
            "-map_metadata", "2",
            "-c:a", "copy", "-c:v", "mjpeg",
            "-disposition:v", "attached_pic",
            "-movflags", "+faststart",
            str(output_m4b),
        ])
    else:
        _run_ffmpeg([
            "ffmpeg", "-y",
            "-i", str(combined_path),
            "-i", str(metadata_file),
            "-map", "0:a",
            "-map_metadata", "1",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(output_m4b),
        ])

    if cleanup:
        combined_path.unlink()

    _validate_m4b(str(output_m4b), len(wav_paths))

    return str(output_m4b)


def _run_ffmpeg(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ffmpeg error: {result.stderr}")
        sys.exit(1)


def _get_duration_ms(path):
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "json", path],
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout)
    duration_sec = float(data["format"]["duration"])
    return int(duration_sec * 1000)


def _validate_m4b(path, expected_chapters):
    if not Path(path).exists() or Path(path).stat().st_size == 0:
        print("M4B validation failed: file missing or empty")
        sys.exit(1)

    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_chapters", "-of", "json", path],
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout)
    actual_chapters = len(data.get("chapters", []))

    file_size = Path(path).stat().st_size
    duration_ms = _get_duration_ms(path)
    total_seconds = duration_ms / 1000
    hours, remainder = divmod(int(total_seconds), 3600)
    minutes, seconds = divmod(remainder, 60)

    print("M4B validation:")
    print(f"  Size: {file_size / (1024 * 1024):.1f} MB")
    print(f"  Duration: {hours}h {minutes}m {seconds}s")
    print(f"  Chapters: {actual_chapters} (expected {expected_chapters})")


def write_output(m4b_path, metadata, cover_image, output_dir):
    author = sanitize_filename(metadata["author"])
    title = sanitize_filename(metadata["title"])
    book_dir = Path(output_dir) / author / title
    book_dir.mkdir(parents=True, exist_ok=True)

    dest_m4b = book_dir / f"{title}.m4b"
    Path(m4b_path).rename(dest_m4b)

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
        f"AI Narration ({metadata.get('voice', 'af_heart')})",
        encoding="utf-8",
    )

    return str(book_dir)


def serve_output(output_dir, port):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=output_dir, **kwargs)

    server = http.server.HTTPServer(("0.0.0.0", port), Handler)
    print(f"\nServing output at http://0.0.0.0:{port}/")
    print("Press Ctrl+C to stop the server.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


def _run_conversion(chapters, args, metadata, cover_image, work_dir, cleanup=True):
    if args.build_only:
        wav_paths = _collect_existing_wavs(chapters, work_dir)
    else:
        _wait_for_tts(args.tts_url)
        print(f"\nSynthesizing {len(chapters)} chapters with voice '{args.voice}'...")
        wav_paths = synthesize_chapters(
            chapters, args.tts_url, args.voice, work_dir, start_chapter=args.start_chapter
        )

    if not wav_paths:
        print("No audio generated.")
        sys.exit(1)

    print("\nBuilding M4B...")
    m4b_path = build_m4b(wav_paths, metadata, cover_image, work_dir, cleanup=cleanup)

    print("\nWriting output...")
    return write_output(m4b_path, metadata, cover_image, args.output)


def _collect_existing_wavs(chapters, work_dir):
    wav_paths = []
    for ch_idx, (title, _) in enumerate(chapters, 1):
        wav_path = Path(work_dir) / f"chapter_{ch_idx:03d}.wav"
        if wav_path.exists():
            wav_paths.append((title, str(wav_path)))
        else:
            print(f"  Missing: chapter_{ch_idx:03d}.wav ({title})")
    print(f"Found {len(wav_paths)}/{len(chapters)} chapter WAVs")
    return wav_paths


def main():
    parser = argparse.ArgumentParser(description="Convert EPUB to M4B audiobook via Kokoro TTS")
    parser.add_argument("epub_path", help="Path to EPUB or KEPUB file")
    parser.add_argument(
        "--tts-url",
        default=os.environ.get("TTS_URL", "http://192.168.2.38:11880"),
    )
    parser.add_argument("--voice", default="af_heart")
    parser.add_argument("--output", default="./output")
    parser.add_argument(
        "--serve",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--port", type=int, default=8590)
    parser.add_argument("--debug", action="store_true", help="Keep intermediate files in output dir")
    parser.add_argument("--start-chapter", type=int, default=1, help="Chapter number to start from (1-based)")
    parser.add_argument("--build-only", action="store_true", help="Skip synthesis, build M4B from existing WAVs")
    args = parser.parse_args()

    epub_path = Path(args.epub_path)
    if not epub_path.exists():
        print(f"File not found: {epub_path}")
        sys.exit(1)

    nltk.download("punkt_tab", quiet=True)

    print(f"Parsing: {epub_path.name}")
    metadata, chapters, cover_image = parse_epub(str(epub_path))
    metadata["voice"] = args.voice

    if not chapters:
        print("No chapters found with sufficient text.")
        sys.exit(1)

    output_path = Path(args.output)
    output_path.mkdir(parents=True, exist_ok=True)

    build_dir = output_path / "_build" / sanitize_filename(metadata["title"])
    use_cache = args.debug or args.build_only
    if use_cache:
        build_dir.mkdir(parents=True, exist_ok=True)
        book_dir = _run_conversion(chapters, args, metadata, cover_image, str(build_dir), cleanup=not args.debug)
    else:
        with tempfile.TemporaryDirectory(dir=str(output_path)) as tmp_dir:
            book_dir = _run_conversion(chapters, args, metadata, cover_image, tmp_dir, cleanup=True)

    title = sanitize_filename(metadata["title"])

    print("\nConversion complete.")
    print(f"  Title:    {metadata['title']}")
    print(f"  Author:   {metadata['author']}")
    print(f"  Chapters: {len(chapters)}")
    print(f"  Output:   {book_dir}")

    if args.serve:
        m4b_filename = f"{title}.m4b"
        listen_url = f"http://localhost:{args.port}/{quote(m4b_filename)}"
        print(f"\n  Listen: {listen_url}")
        serve_output(book_dir, args.port)


if __name__ == "__main__":
    main()
