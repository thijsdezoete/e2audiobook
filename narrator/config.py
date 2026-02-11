import os
import re
import sqlite3
from dataclasses import dataclass, field

DEFAULTS = {
    "tts_url": "http://kokoro-tts:8880",
    "calibre_library_path": "/calibre-library",
    "audiobook_output_path": "/audiobooks",
    "default_voice": "af_heart",
    "db_path": "/app/data/narrator.db",
    "log_level": "info",
    "narrator_port": "8585",
    "tts_speed": "1.0",
    "auto_convert": "false",
    "auto_scan_interval": "300",
    "abs_api_url": "",
    "abs_api_token": "",
    "webhook_url": "",
    "webhook_on_complete": "true",
    "webhook_on_failure": "true",
    "quiet_hours_start": "",
    "quiet_hours_end": "",
    "delay_between_books": "0",
}

ENV_MAP = {
    "tts_url": "TTS_API_URL",
    "calibre_library_path": "CALIBRE_LIBRARY_PATH",
    "audiobook_output_path": "AUDIOBOOK_OUTPUT_PATH",
    "default_voice": "DEFAULT_VOICE",
    "db_path": "NARRATOR_DB_PATH",
    "log_level": "LOG_LEVEL",
    "narrator_port": "NARRATOR_PORT",
    "tts_speed": "TTS_SPEED",
    "auto_convert": "AUTO_CONVERT",
    "auto_scan_interval": "AUTO_SCAN_INTERVAL",
    "abs_api_url": "ABS_API_URL",
    "abs_api_token": "ABS_API_TOKEN",
    "webhook_url": "WEBHOOK_URL",
    "webhook_on_complete": "WEBHOOK_ON_COMPLETE",
    "webhook_on_failure": "WEBHOOK_ON_FAILURE",
    "quiet_hours_start": "QUIET_HOURS_START",
    "quiet_hours_end": "QUIET_HOURS_END",
    "delay_between_books": "DELAY_BETWEEN_BOOKS",
}


@dataclass
class Settings:
    tts_url: str = ""
    calibre_library_path: str = ""
    audiobook_output_path: str = ""
    default_voice: str = ""
    db_path: str = ""
    log_level: str = ""
    narrator_port: str = ""
    tts_speed: str = ""
    auto_convert: str = ""
    auto_scan_interval: str = ""
    abs_api_url: str = ""
    abs_api_token: str = ""
    webhook_url: str = ""
    webhook_on_complete: str = ""
    webhook_on_failure: str = ""
    quiet_hours_start: str = ""
    quiet_hours_end: str = ""
    delay_between_books: str = ""

    token_limit: int = 250
    token_floor: int = 80
    chars_per_token: float = 3.5
    min_chapter_words: int = 50
    fallback_chapter_words: int = 5000
    crossfade_ms: int = 50
    aac_bitrate: str = "128k"
    max_retries: int = 5
    retry_backoff: tuple[int, ...] = field(default_factory=lambda: (5, 10, 20, 40, 60))
    tts_startup_timeout: int = 300
    tts_cooldown: float = 1.0
    tts_rest_interval: int = 10
    tts_rest_duration: int = 5

    _db_conn: sqlite3.Connection | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        for key, env_name in ENV_MAP.items():
            env_val = os.environ.get(env_name)
            if env_val is not None:
                setattr(self, key, env_val)
            elif not getattr(self, key):
                setattr(self, key, DEFAULTS.get(key, ""))

    def bind_db(self, conn: sqlite3.Connection):
        self._db_conn = conn
        self._populate_defaults(conn)

    def _populate_defaults(self, conn: sqlite3.Connection):
        existing = {
            row[0] for row in conn.execute("SELECT key FROM settings").fetchall()
        }
        for key in DEFAULTS:
            if key not in existing:
                val = getattr(self, key, DEFAULTS[key])
                conn.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                    (key, val),
                )
        conn.commit()

    def get(self, key: str) -> str:
        if self._db_conn:
            try:
                row = self._db_conn.execute(
                    "SELECT value FROM settings WHERE key = ?", (key,)
                ).fetchone()
                if row:
                    return row[0]
            except sqlite3.OperationalError:
                pass
        val = getattr(self, key, None)
        if val is not None:
            return str(val)
        return DEFAULTS.get(key, "")

    def set(self, key: str, value: str):
        if self._db_conn:
            self._db_conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
            self._db_conn.commit()
        if hasattr(self, key) and key in DEFAULTS:
            setattr(self, key, value)

    def get_all(self) -> dict[str, str]:
        result = {}
        for key in DEFAULTS:
            result[key] = self.get(key)
        return result

    def update(self, values: dict[str, str]):
        for key, value in values.items():
            if key in DEFAULTS:
                self.set(key, value)


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


Config = Settings
