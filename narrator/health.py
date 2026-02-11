import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

log = logging.getLogger(__name__)


@dataclass
class HealthState:
    kokoro_connected: bool = False
    kokoro_voices: int = 0
    kokoro_last_check: float = 0
    library_accessible: bool = False
    output_writable: bool = False
    worker_running: bool = False
    queue_paused: bool = False
    uptime_start: float = field(default_factory=time.monotonic)

    @property
    def overall(self) -> str:
        if self.kokoro_connected and self.library_accessible and self.output_writable:
            return "healthy"
        if self.library_accessible:
            return "degraded"
        return "unhealthy"

    @property
    def uptime_seconds(self) -> int:
        return int(time.monotonic() - self.uptime_start)

    def to_dict(self) -> dict:
        return {
            "status": self.overall,
            "uptime_seconds": self.uptime_seconds,
            "kokoro": {
                "connected": self.kokoro_connected,
                "voices": self.kokoro_voices,
            },
            "library": {"accessible": self.library_accessible},
            "output": {"writable": self.output_writable},
            "worker": {
                "running": self.worker_running,
                "queue_paused": self.queue_paused,
            },
        }


state = HealthState()


async def health_monitor(tts_url: str, library_path: str, output_path: str, interval: int = 60):
    while True:
        try:
            await _check_kokoro(tts_url)
            _check_library(library_path)
            _check_output(output_path)
        except Exception:
            log.exception("Health check error")
        await asyncio.sleep(interval)


async def _check_kokoro(tts_url: str):
    try:
        resp = await asyncio.to_thread(
            requests.get, f"{tts_url}/v1/audio/voices", timeout=10
        )
        resp.raise_for_status()
        voices = resp.json()
        state.kokoro_connected = True
        state.kokoro_voices = len(voices) if isinstance(voices, list) else 0
    except Exception:
        state.kokoro_connected = False
        state.kokoro_voices = 0
    state.kokoro_last_check = time.monotonic()


def _check_library(library_path: str):
    p = Path(library_path)
    state.library_accessible = p.exists() and p.is_dir()


def _check_output(output_path: str):
    p = Path(output_path)
    if not p.exists():
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError:
            state.output_writable = False
            return
    state.output_writable = p.is_dir() and _is_writable(p)


def _is_writable(path: Path) -> bool:
    test_file = path / ".narrator_write_test"
    try:
        test_file.write_text("test")
        test_file.unlink()
        return True
    except OSError:
        return False
