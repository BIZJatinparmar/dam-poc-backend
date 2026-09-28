#!/bin/sh
set -eu

STARTUP_LOG=/home/LogFiles/atlas-startup.log
mkdir -p /home/LogFiles
echo "[startup] starting database migration" >> "$STARTUP_LOG"
if ! python -m alembic upgrade head >> "$STARTUP_LOG" 2>&1; then
    echo "[startup] database migration failed; see $STARTUP_LOG" >&2
    exit 1
fi
echo "[startup] migration finished; starting API" >> "$STARTUP_LOG"
exec python -m uvicorn atlas_backend.app:app --host 0.0.0.0 --port 8000
