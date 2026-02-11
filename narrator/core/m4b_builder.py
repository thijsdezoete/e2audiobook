import io
import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from narrator.config import Config, sanitize_filename

log = logging.getLogger(__name__)


class M4BBuildError(Exception):
    pass


@dataclass
class ValidationResult:
    path: str
    size_bytes: int
    duration_ms: int
    actual_chapters: int
    expected_chapters: int

    @property
    def size_mb(self) -> float:
        return self.size_bytes / (1024 * 1024)

    @property
    def duration_str(self) -> str:
        total_seconds = self.duration_ms / 1000
        hours, remainder = divmod(int(total_seconds), 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours}h {minutes}m {seconds}s"


class M4BBuilder:
    def __init__(self, config: Config):
        self.aac_bitrate = config.aac_bitrate

    def build(
        self,
        wav_paths: list[tuple[str, str]],
        metadata: dict,
        cover_image: bytes | None,
        tmp_dir: str,
        cleanup: bool = True,
    ) -> str:
        tmp = Path(tmp_dir)
        m4a_paths = []

        for title, wav_path in wav_paths:
            m4a_path = Path(wav_path).with_suffix(".m4a")
            _run_ffmpeg([
                "ffmpeg", "-y", "-i", wav_path,
                "-c:a", "aac", "-b:a", self.aac_bitrate,
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

        return str(output_m4b)

    def validate(self, path: str, expected_chapters: int) -> ValidationResult:
        p = Path(path)
        if not p.exists() or p.stat().st_size == 0:
            raise M4BBuildError("M4B validation failed: file missing or empty")

        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_chapters", "-of", "json", path],
            capture_output=True,
            text=True,
        )
        data = json.loads(result.stdout)
        actual_chapters = len(data.get("chapters", []))

        file_size = p.stat().st_size
        duration_ms = _get_duration_ms(path)

        vr = ValidationResult(
            path=path,
            size_bytes=file_size,
            duration_ms=duration_ms,
            actual_chapters=actual_chapters,
            expected_chapters=expected_chapters,
        )

        log.info("M4B validation:")
        log.info("  Size: %.1f MB", vr.size_mb)
        log.info("  Duration: %s", vr.duration_str)
        log.info("  Chapters: %d (expected %d)", vr.actual_chapters, vr.expected_chapters)

        return vr


def _run_ffmpeg(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise M4BBuildError(f"ffmpeg error: {result.stderr}")


def _get_duration_ms(path):
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "json", path],
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout)
    duration_sec = float(data["format"]["duration"])
    return int(duration_sec * 1000)
