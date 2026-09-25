"""Blob integration check; run with the compose azurite profile enabled."""

import os
import subprocess
from uuid import uuid4

import pytest
from azure.storage.blob import BlobServiceClient

from atlas_backend import app as app_module
from atlas_backend.analysis import run_analysis_step
from atlas_backend.storage import BlobStorage
from test_api import FakeAzure, client  # noqa: F401 - shared FastAPI fixture


@pytest.mark.skipif(os.getenv("ATLAS_TEST_AZURITE") != "1", reason="Azurite not enabled")
def test_upload_and_ranged_read_use_private_blob(client, monkeypatch, tmp_path):
    http, engine, _ = client
    service = BlobServiceClient.from_connection_string("UseDevelopmentStorage=true", api_version="2023-11-03")
    container_name = f"atlas-test-{uuid4().hex[:12]}"
    container = service.create_container(container_name)
    try:
        monkeypatch.setattr(app_module, "storage", BlobStorage(container))
        created = http.post("/api/upload", headers={"X-Demo-User": "alex"},
                            files={"file": ("sample.png", b"\x89PNG\r\n\x1a\nexample", "image/png")})
        assert created.status_code == 201
        url = created.json()["url"]
        whole = http.get(url)
        partial = http.get(url, headers={"Range": "bytes=2-5"})
        assert whole.status_code == 200
        assert partial.status_code == 206
        assert partial.content == whole.content[2:6]
        assert partial.headers["content-range"] == f"bytes 2-5/{len(whole.content)}"
        assert http.get(f"{url}&download=1").headers["content-disposition"].startswith("attachment;")
        assert run_analysis_step(engine, app_module.storage, FakeAzure("image"))
        assert run_analysis_step(engine, app_module.storage, FakeAzure("image"))
        assert http.get(f"/api/assets/{created.json()['id']}", headers={"X-Demo-User": "alex"}).json()["analysis_state"] == "complete"

        video = tmp_path / "sample.mp4"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                        "color=c=red:s=320x180:d=1", "-c:v", "mpeg4", "-y", str(video)],
                       check=True, capture_output=True)
        uploaded = http.post("/api/upload", headers={"X-Demo-User": "alex"},
                             files={"file": ("sample.mp4", video.read_bytes(), "video/mp4")})
        assert uploaded.status_code == 201
        assert http.get(uploaded.json()["thumbnail_url"]).content.startswith(b"\xff\xd8")
        media = http.get(uploaded.json()["url"], headers={"Range": "bytes=0-3"})
        assert media.status_code == 206
        assert len(media.content) == 4
    finally:
        container.delete_container()
