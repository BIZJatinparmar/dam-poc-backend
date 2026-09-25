"""Read duration from the media container rather than the speech transcript."""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path


def probe_video_duration(path: Path) -> float | None:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30, check=False,
        )
        seconds = float(json.loads(result.stdout).get("format", {}).get("duration", ""))
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, KeyError, AttributeError):
        return None
    return seconds if result.returncode == 0 and math.isfinite(seconds) and seconds > 0 else None


def duration_label(seconds: float) -> str:
    total = round(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
