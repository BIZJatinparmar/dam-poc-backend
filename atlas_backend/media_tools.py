"""Locate the FFmpeg executables shipped with the Linux App Service artifact."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from functools import lru_cache
from pathlib import Path


_BUNDLED_BIN = Path(__file__).resolve().parents[1] / "vendor" / "ffmpeg" / "linux-x64"


@lru_cache(maxsize=2)
def media_tool(name: str) -> str:
    if name not in {"ffmpeg", "ffprobe"}:
        raise ValueError(f"Unsupported media tool: {name}")
    if sys.platform != "linux":
        return name

    bundled = _BUNDLED_BIN / name
    if not bundled.is_file():
        return name  # Local development can use the system package.

    if not os.access(bundled, os.X_OK):
        try:
            bundled.chmod(bundled.stat().st_mode | 0o111)
        except OSError:
            # Some deployment layouts mount the application read only.
            executable_dir = Path(tempfile.mkdtemp(prefix="atlas-ffmpeg-"))
            bundled = Path(shutil.copy2(bundled, executable_dir / name))
            bundled.chmod(bundled.stat().st_mode | 0o111)
    return str(bundled)
