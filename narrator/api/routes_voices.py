import asyncio
import logging
import time

import requests
from fastapi import APIRouter

router = APIRouter()
log = logging.getLogger(__name__)

_voice_cache: list[str] = []
_cache_time: float = 0
CACHE_TTL = 300


@router.get("")
async def list_voices():
    from narrator.app import settings

    global _voice_cache, _cache_time
    if _voice_cache and (time.monotonic() - _cache_time) < CACHE_TTL:
        return {"voices": _voice_cache}

    tts_url = settings.get("tts_url")
    try:
        resp = await asyncio.to_thread(
            requests.get, f"{tts_url}/v1/audio/voices", timeout=10
        )
        resp.raise_for_status()
        voices = resp.json()
        if isinstance(voices, list):
            _voice_cache = voices
        elif isinstance(voices, dict) and "voices" in voices:
            _voice_cache = voices["voices"]
        else:
            _voice_cache = []
        _cache_time = time.monotonic()
    except Exception as e:
        log.warning("Failed to fetch voices: %s", e)
        if not _voice_cache:
            return {"voices": [], "error": str(e)}

    return {"voices": _voice_cache}


@router.post("/refresh")
async def refresh_voices():
    global _voice_cache, _cache_time
    _voice_cache = []
    _cache_time = 0
    return await list_voices()
