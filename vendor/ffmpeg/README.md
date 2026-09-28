# FFmpeg for the Linux App Service

The GitHub Actions deployment workflow downloads the pinned Linux x86-64
FFmpeg 9.0 build from [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds/releases/tag/autobuild-2026-09-01-13-13), verifies its published SHA-256 checksum, and places `ffmpeg` and `ffprobe` in `vendor/ffmpeg/linux-x64/` before creating the deployment artifact. These binaries are included in the deployed Python app, not committed to Git.

The backend selects these executables automatically on Linux. Local development without a bundle uses `ffmpeg` and `ffprobe` from `PATH`. The bundled build is the LGPL variant; review [FFmpeg's legal information](https://ffmpeg.org/legal.html) when distributing it.
