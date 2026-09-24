#!/bin/sh
set -eu
alembic upgrade head
exec python -m uvicorn atlas_backend.app:app --host 0.0.0.0 --port 8000
