# Atlas backend

FastAPI, PostgreSQL, and asset storage for the Atlas DAM demo. This repository is independent of the React frontend. The API serves `/api`, `/uploads`, `/thumbnails`, and `/demo-art`; it no longer serves React files.

## Local development

Requires Python 3.14, Docker Compose, and FFmpeg (`ffmpeg` and `ffprobe` on `PATH`). Compose now uses a PostgreSQL 17 image with pgvector. From this repository:

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

Keep Content Understanding settings server-side for image tags. Video uploads use FFmpeg to extract FLAC audio, Azure Speech batch transcription for phrase timestamps, and a Foundry model for transcript-grounded tags. Set the Speech and Foundry settings in `.env.example` using protected App Service settings or Key Vault references. The Speech resource needs a system-assigned managed identity with `Storage Blob Data Reader` on the private temporary audio container. Give that Speech resource access through the storage firewall's resource-instance rule. The backend identity needs `Storage Blob Data Contributor` on the temporary container. Configure a Blob lifecycle rule to delete temporary audio after two days; the worker also deletes it after transcription. Local video analysis still needs a real Azure Blob container because Speech cannot fetch audio from Azurite.

Set `ATLAS_SPEECH_LOCALE` to the expected English locale (`en-US` by default). The configured Foundry deployment should use `gpt-5.4-nano` with structured outputs. Tag generation failures leave the transcript available and expose a tag-only retry. Before enabling the replacement in production, compare your Azure contract's Video Indexer rate with Speech batch audio duration, Foundry input/output tokens, Blob operations/storage, and audio extraction compute for a representative set of uploads. To create the Content Understanding analyzer, run `python -m atlas_backend.configure_image_analyzer` with the Azure settings configured.

Run only one backend process and one App Service instance for this demo. The analysis worker polls PostgreSQL but has no distributed claim lock. `X-Demo-User` is a role switcher, not authentication. Use access restrictions and do not upload real customer assets.

## Discovery

Discovery stores embeddings in PostgreSQL with pgvector. Deploy `text-embedding-3-small` at its default 1536 dimensions and configure `ATLAS_EMBEDDING_ENDPOINT`, `ATLAS_EMBEDDING_KEY`, and `ATLAS_EMBEDDING_DEPLOYMENT` (the endpoint and key may reuse the Foundry resource). Existing `ATLAS_FOUNDRY_*` settings power cited chat answers. Set a persistent `ATLAS_MEDIA_SIGNING_KEY` in App Service so short-lived video and thumbnail links continue working across restarts. Search and chat return 503 if embeddings are unavailable; the asset library still works.

Migration `0003_semantic_discovery` installs the `vector` extension and index tables. Azure PostgreSQL Flexible Server must allow-list the `vector` extension before migration. The single-process worker discovers existing assets automatically, indexes transcript windows and video metadata, and indexes image tags. New uploads and edits are marked pending in the same transaction. Discovery's Indexing activity lists pending or failed assets; editors can retry failures there. Until an asset's current version is ready, stale vectors are excluded from search.

Discovery search is available at `POST /api/discovery/search` with `{ "query": "...", "media_type": "all|video|image", "status": "all|draft|in_review|approved|published" }`. `POST /api/discovery/chat` also accepts `message` and up to six recent `{role,content}` messages; history is kept by the browser session. Responses include asset summaries and cited match IDs with numeric video start/end seconds. After provisioning a missing embedding deployment, an admin can use **Retry all failed** on the Discovery page (or `POST /api/discovery/retry-failed`) to requeue the backfill. Media URLs expire after one hour and are rechecked against the current demo user's visibility; the demo user header still does not provide real authentication.

See `DEPLOYMENT.md` for App Service preparation.

## Video dashboard

Migration `0004_video_dashboard` stores exact upload sizes and controlled video topic and format labels. New videos are classified from their spoken transcripts after Speech transcription; the model must return a supporting excerpt from the transcript. Silent videos remain unclassified. Editors can correct labels in the asset drawer, and manual corrections are preserved on later analysis runs.

The Overview dashboard groups visible videos by primary topic, format, and workflow status. Storage totals use exact byte counts. At startup, the backend reads local file or Blob metadata to backfill sizes for older uploads; videos whose originals cannot be found remain marked as unmeasured. Existing completed videos with transcripts are queued for classification once after migration. A failed classification leaves the transcript and tags available and can be retried from the asset drawer or with **Retry failed** on Overview. The model cites a transcript segment ID; the backend copies the supporting text from that segment.

Migration `0005_video_duration` stores video duration in seconds. FFprobe measures new MP4 uploads; on startup the backend probes older originals once to fill missing durations. The Overview average includes only videos with a measured duration and shows how many videos were measured.
