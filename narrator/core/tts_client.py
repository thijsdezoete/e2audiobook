import io
import logging
import time
from collections.abc import Callable
from pathlib import Path

import nltk
import requests
from pydub import AudioSegment

from narrator.config import Config

log = logging.getLogger(__name__)


class TTSConnectionError(Exception):
    pass


class TTSSynthesisError(Exception):
    pass


def chunk_text(text: str, limit: int = 250, chars_per_token: float = 3.5, token_floor: int = 80) -> list[str]:
    sentences = nltk.sent_tokenize(text)
    chunks: list[str] = []
    current_chunk: list[str] = []
    current_tokens = 0.0

    for sentence in sentences:
        token_est = len(sentence) / chars_per_token
        if token_est > limit:
            if current_chunk:
                chunks.append(" ".join(current_chunk))
                current_chunk = []
                current_tokens = 0
            chunks.extend(_split_long_sentence(sentence, limit, chars_per_token))
            continue

        if current_tokens + token_est > limit and current_tokens >= token_floor:
            chunks.append(" ".join(current_chunk))
            current_chunk = []
            current_tokens = 0

        current_chunk.append(sentence)
        current_tokens += token_est

    if current_chunk:
        tail = " ".join(current_chunk)
        tail_tokens = len(tail) / chars_per_token
        if chunks and tail_tokens < token_floor:
            chunks[-1] = chunks[-1] + " " + tail
        else:
            chunks.append(tail)

    return chunks


def _split_long_sentence(sentence: str, limit: int, chars_per_token: float) -> list[str]:
    target_chars = int(limit * chars_per_token * 0.9)
    parts: list[str] = []

    while len(sentence) / chars_per_token > limit:
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


class TTSClient:
    def __init__(self, config: Config):
        self.url = config.tts_url
        self.max_retries = config.max_retries
        self.retry_backoff = config.retry_backoff
        self.startup_timeout = config.tts_startup_timeout
        self.cooldown = config.tts_cooldown
        self.rest_interval = config.tts_rest_interval
        self.rest_duration = config.tts_rest_duration
        self.token_limit = config.token_limit
        self.token_floor = config.token_floor
        self.chars_per_token = config.chars_per_token
        self.crossfade_ms = config.crossfade_ms

    def wait_until_ready(self):
        log.info("Waiting for TTS server at %s...", self.url)
        deadline = time.monotonic() + self.startup_timeout
        interval = 5
        while time.monotonic() < deadline:
            try:
                resp = requests.get(f"{self.url}/v1/audio/voices", timeout=10)
                resp.raise_for_status()
                voices = resp.json()
                voice_count = len(voices) if isinstance(voices, list) else "unknown"
                log.info("TTS server responding (%s voices available)", voice_count)
                break
            except requests.RequestException:
                remaining = int(deadline - time.monotonic())
                log.info("TTS server not ready, retrying in %ds (%ds remaining)...", interval, remaining)
                time.sleep(interval)
        else:
            raise TTSConnectionError(f"TTS server not reachable after {self.startup_timeout}s")

        self._warmup()

    def _warmup(self):
        warmup_text = (
            "This is a warmup request to initialize the text to speech model. "
            "The quick brown fox jumps over the lazy dog near the bank of a quiet river. "
            "She sells seashells by the seashore while the waves crash gently on the sand."
        )
        for attempt in range(3):
            log.info("Warming up TTS model (attempt %d/3)...", attempt + 1)
            try:
                warmup = requests.post(
                    f"{self.url}/v1/audio/speech",
                    json={"model": "kokoro", "input": warmup_text, "voice": "af_heart", "response_format": "wav"},
                    timeout=60,
                )
                warmup.raise_for_status()
                time.sleep(5)
                log.info("TTS server ready")
                return
            except requests.RequestException as e:
                log.warning("Warmup failed (%s), restarting health check...", e)
                time.sleep(15)
                deadline = time.monotonic() + self.startup_timeout
                while time.monotonic() < deadline:
                    try:
                        resp = requests.get(f"{self.url}/v1/audio/voices", timeout=10)
                        resp.raise_for_status()
                        break
                    except requests.RequestException:
                        time.sleep(5)
        raise TTSConnectionError("TTS server failed to stabilize after warmup attempts")

    def synthesize_chapter(
        self,
        title: str,
        text: str,
        voice: str,
        output_path: Path,
        chapter_num: int = 0,
        total_chapters: int = 0,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> Path:
        if output_path.exists():
            log.info("Chapter %d/%d: %s [cached]", chapter_num, total_chapters, title)
            return output_path

        spoken_title = title.title() if title.isupper() else title
        chunks = [
            f"{spoken_title}.",
            *chunk_text(
                text, limit=self.token_limit, chars_per_token=self.chars_per_token, token_floor=self.token_floor
            ),
        ]
        total_chunks = len(chunks)
        segments: list[AudioSegment] = []

        for chunk_idx, chunk in enumerate(chunks, 1):
            if chunk_idx > 1 and (chunk_idx - 1) % self.rest_interval == 0:
                log.info("Resting %ds to let TTS recover VRAM...", self.rest_duration)
                time.sleep(self.rest_duration)
            log.info("Chapter %d/%d -- Chunk %d/%d: %s", chapter_num, total_chapters, chunk_idx, total_chunks, title)
            audio_bytes = self._request(chunk, voice)
            segment = AudioSegment.from_wav(io.BytesIO(audio_bytes))
            segments.append(segment)
            if chunk_idx < total_chunks:
                time.sleep(self.cooldown)

        if not segments:
            raise TTSSynthesisError(f"No audio segments produced for chapter: {title}")

        combined = segments[0]
        for segment in segments[1:]:
            combined = combined.append(segment, crossfade=self.crossfade_ms)

        combined.export(str(output_path), format="wav")

        if on_progress:
            on_progress(chapter_num, total_chapters)

        return output_path

    def _request(self, text: str, voice: str) -> bytes:
        url = f"{self.url}/v1/audio/speech"
        payload = {
            "model": "kokoro",
            "input": text,
            "voice": voice,
            "response_format": "wav",
        }

        for attempt in range(self.max_retries):
            try:
                response = requests.post(url, json=payload, timeout=120)
                response.raise_for_status()
                return response.content
            except (requests.RequestException, requests.HTTPError) as e:
                if attempt < self.max_retries - 1:
                    log.warning("TTS request failed (attempt %d/%d): %s", attempt + 1, self.max_retries, e)
                    log.info("Waiting for TTS server to recover...")
                    self.wait_until_ready()
                else:
                    raise TTSSynthesisError(
                        f"TTS request failed after {self.max_retries} attempts: {e}"
                    ) from e
        raise TTSSynthesisError("TTS request failed unexpectedly")
