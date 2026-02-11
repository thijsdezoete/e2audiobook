import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    tts_url: str = os.environ.get("TTS_API_URL", "http://kokoro-tts:8880")
    calibre_library_path: str = os.environ.get("CALIBRE_LIBRARY_PATH", "/calibre-library")
    audiobook_output_path: str = os.environ.get("AUDIOBOOK_OUTPUT_PATH", "/audiobooks")
    default_voice: str = os.environ.get("DEFAULT_VOICE", "af_heart")
    db_path: str = os.environ.get("NARRATOR_DB_PATH", "/app/data/narrator.db")

    token_limit: int = 250
    token_floor: int = 80
    chars_per_token: float = 3.5
    min_chapter_words: int = 50
    fallback_chapter_words: int = 5000
    crossfade_ms: int = 50
    aac_bitrate: str = "128k"
    max_retries: int = 5
    retry_backoff: tuple[int, ...] = (5, 10, 20, 40, 60)
    tts_startup_timeout: int = 300
    tts_cooldown: float = 1.0
    tts_rest_interval: int = 10
    tts_rest_duration: int = 5

    log_level: str = os.environ.get("LOG_LEVEL", "info").upper()


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


def sanitize_filename(name: str) -> str:
    return SANITIZE_CHARS.sub("_", name).strip()
