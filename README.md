# Atlas backend

FastAPI, PostgreSQL, and asset storage for the Atlas DAM demo. This repository is independent of the React frontend. The API serves `/api`, `/uploads`, `/thumbnails`, and `/demo-art`; it no longer serves React files.

## Local development

Requires Python 3.14, Docker Compose, and FFmpeg (`ffmpeg` and `ffprobe` on `PATH`). From this repository:

```powershell
docker compose up -d db
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m uvicorn atlas_backend.app:app --host 127.0.0.1 --port 8000
```

The local default is PostgreSQL at `127.0.0.1:5432` with database, user, and password `atlas`. Local originals and thumbnails go into `data/uploads`; this folder is ignored by Git. Copy `.env.example` to `.env` to override settings. Existing legacy data in the parent workspace is untouched and is not imported. API docs are at <http://127.0.0.1:8000/docs>.

In another terminal, run the sibling frontend with `npm run dev`. The default allowed browser origin is `http://127.0.0.1:5173`.

## Checks

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
$env:ATLAS_TEST_DATABASE_URL='postgresql+psycopg://atlas:atlas@127.0.0.1:5432/atlas_test'
.\.venv\Scripts\python.exe -m pytest tests -q
Remove-Item Env:ATLAS_TEST_DATABASE_URL
```

To exercise Blob Storage against Azurite, run `docker compose --profile blob-test up -d`, set `ATLAS_TEST_AZURITE=1`, and run `pytest tests/test_blob_storage.py`. This test creates and removes its own private test container.

## Configuration

| Setting | Use |
| --- | --- |
| `DATABASE_URL` | PostgreSQL SQLAlchemy URL; use `?sslmode=require` in Azure |
| `ATLAS_STORAGE_BACKEND` | `local` during development, `blob` in Azure |
| `ATLAS_DATA_DIR` | Local development data directory |
| `ATLAS_BLOB_ACCOUNT_URL`, `ATLAS_BLOB_CONTAINER` | Private Blob container in Azure |
| `ATLAS_BLOB_CONNECTION_STRING` | Azurite tests only; Azure uses managed identity |
| `ATLAS_ALLOWED_ORIGINS` | Comma-separated exact browser origins for CORS |
| `ATLAS_ANALYSIS_WORKER` | `1` runs the single-process analysis worker; `0` disables it |

Keep Content Understanding, Video Indexer, and service principal settings from the legacy local `.env` server-side. App Service managed identity is used for Video Indexer when available. To create the Content Understanding analyzer, run `python -m atlas_backend.configure_image_analyzer` with the Azure settings configured.

Run only one backend process and one App Service instance for this demo. The analysis worker polls PostgreSQL but has no distributed claim lock. `X-Demo-User` is a role switcher, not authentication. Use access restrictions and do not upload real customer assets.

See `DEPLOYMENT.md` for App Service preparation.
