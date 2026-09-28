#!/bin/sh
set -eu
echo "[startup] starting database migration"
alembic upgrade head
echo "[startup] migration finished; starting API"
exec python -m uvicorn atlas_backend.app:app --host 0.0.0.0 --port 8000