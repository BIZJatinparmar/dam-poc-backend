"""Create a still image from an uploaded MP4 for asset listings."""

import subprocess
from pathlib import Path

from .media_tools import media_tool


def ensure_video_thumbnail(video_path: Path) -> Path | None:
    thumbnail = video_path.with_suffix(".jpg")
    if thumbnail.is_file():
        return thumbnail
    if not video_path.is_file():
        return None

    # A frame shortly after the start is usually more useful than a fade-in frame.
    for seek in ("0.5", "0"):
        try:
            result = subprocess.run(
                [media_tool("ffmpeg"), "-loglevel", "error", "-y", "-ss", seek, "-i", str(video_path),
                 "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "3", str(thumbnail)],
                capture_output=True, timeout=30, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            thumbnail.unlink(missing_ok=True)
            return None
        if result.returncode == 0 and thumbnail.is_file() and thumbnail.stat().st_size:
            return thumbnail
        thumbnail.unlink(missing_ok=True)
    return None
