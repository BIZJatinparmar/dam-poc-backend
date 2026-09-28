# FFmpeg for the Linux App Service

The GitHub Actions deployment workflow downloads the Linux x86-64
FFmpeg 9.0 build from [BtbN/FFmpeg-Builds latest release](https://github.com/BtbN/FFmpeg-Builds/releases/tag/latest), verifies its published SHA-256 checksum, and places `ffmpeg` and `ffprobe` in `vendor/ffmpeg/linux-x64/` before creating the deployment artifact. These binaries are included in the deployed Python app, not committed to Git. The `latest` release changes over time, so deployments may include newer FFmpeg 9.0 builds.

The backend selects these executables automatically on Linux. Local development without a bundle uses `ffmpeg` and `ffprobe` from `PATH`. The bundled build is the LGPL variant; review [FFmpeg's legal information](https://ffmpeg.org/legal.html) when distributing it.
