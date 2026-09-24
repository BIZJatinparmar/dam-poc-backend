import json
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from atlas_backend import app as app_module
from atlas_backend.analysis import AnalysisFailure, run_analysis_step
from atlas_backend.database import Base, get_db
from atlas_backend.models import Activity, Asset
from atlas_backend.storage import LocalStorage


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    test_url = os.getenv("ATLAS_TEST_DATABASE_URL")
    engine = (create_engine(test_url) if test_url else
              create_engine(f"sqlite:///{(tmp_path / 'test.db').as_posix()}", connect_args={"check_same_thread": False}))
    if test_url:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        app_module.seed_database(session)

    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setattr(app_module, "storage", LocalStorage(upload_dir))
    monkeypatch.setattr(app_module, "engine", engine)
    monkeypatch.setattr(app_module, "ANALYSIS_WORKER_ENABLED", False)

    def test_db():
        with Session(engine) as session:
            yield session

    app_module.app.dependency_overrides[get_db] = test_db
    try:
        with TestClient(app_module.app) as test_client:
            yield test_client, engine, upload_dir
    finally:
        app_module.app.dependency_overrides.clear()
        if test_url:
            Base.metadata.drop_all(engine)
        engine.dispose()


def as_user(user_id: str) -> dict[str, str]:
    return {"X-Demo-User": user_id}


def test_cors_preflight_allows_configured_frontend(client):
    http, _, _ = client
    assert http.get("/").json()["status"] == "ok"
    response = http.options("/api/upload", headers={
        "Origin": "http://127.0.0.1:5173",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "x-demo-user",
    })
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"


def add_review_asset(engine, asset_id="review-image"):
    from datetime import date

    with Session(engine) as session:
        session.add(Asset(
            id=asset_id, title="Team on the move", type="image", campaign="Test",
            brand="Northstar", status="in_review", rights=date(2027, 4, 30),
            audience="internal", tags=[], ai_tags=[], description="Test asset",
            owner_id="alex", owner="Alex Morgan", size="1 MB", uploaded="1 Sep 2026",
            art="upload", transcript=[],
        ))
        session.commit()


def test_starter_media_is_absent(client):
    http, engine, _ = client
    assert http.get("/api/assets", headers=as_user("maya")).json() == []
    assert http.get("/api/summary", headers=as_user("maya")).json()["total"] == 0
    with Session(engine) as session:
        assert session.query(Activity).count() == 0


def test_agency_only_sees_approved_external_assets(client):
    http, engine, _ = client
    add_review_asset(engine)
    agency_assets = http.get("/api/assets", headers=as_user("quinn")).json()
    assert len(agency_assets) == 0
    assert all(asset["audience"] == "external" and asset["status"] in {"approved", "published"} for asset in agency_assets)


def test_rejected_editor_approval_does_not_change_metadata(client):
    http, engine, _ = client
    add_review_asset(engine)
    response = http.patch("/api/assets/review-image", headers=as_user("alex"), json={"status": "approved", "title": "Changed"})
    assert response.status_code == 403
    with Session(engine) as session:
        asset = session.get(Asset, "review-image")
        assert asset is not None
        assert asset.title == "Team on the move"
        assert asset.status == "in_review"


def test_approval_persists_and_changes_agency_visibility(client):
    http, engine, _ = client
    add_review_asset(engine)
    response = http.patch("/api/assets/review-image", headers=as_user("maya"), json={"status": "approved", "audience": "external"})
    assert response.status_code == 200
    with Session(engine) as new_session:
        asset = new_session.get(Asset, "review-image")
        assert asset is not None
        assert asset.status == "approved"
    agency_ids = [asset["id"] for asset in http.get("/api/assets", headers=as_user("quinn")).json()]
    assert "review-image" in agency_ids


def test_user_can_be_added_then_disabled(client):
    http, _, _ = client
    created = http.post("/api/users", headers=as_user("maya"), json={"name": "Demo Partner", "email": "partner@example.com", "role": "agency"})
    assert created.status_code == 201
    user_id = created.json()["id"]
    disabled = http.patch(f"/api/users/{user_id}", headers=as_user("maya"), json={"active": False})
    assert disabled.status_code == 200
    assert http.get("/api/assets", headers=as_user(user_id)).status_code == 403


def test_uploaded_file_and_metadata_survive_new_session(client):
    http, engine, upload_dir = client
    created = http.post("/api/upload", headers=as_user("alex"), files={"file": ("sample.png", b"\x89PNG\r\n\x1a\nexample", "image/png")})
    assert created.status_code == 201
    asset_id = created.json()["id"]
    with Session(engine) as new_session:
        asset = new_session.get(Asset, asset_id)
        assert asset is not None
        assert asset.status == "draft"
        assert (upload_dir / asset.file_name).is_file()
    assert http.get(f"/uploads/{asset_id}").status_code == 200
    assert http.get(f"/uploads/{asset_id}", headers={"Range": "bytes=0-3"}).status_code == 206


def test_video_thumbnail_and_processed_upload_survive_starter_cleanup(client):
    http, engine, upload_dir = client
    source = upload_dir / "source.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=320x180:d=1",
         "-c:v", "mpeg4", "-y", str(source)], check=True, capture_output=True,
    )
    created = http.post("/api/upload", headers=as_user("alex"),
                        files={"file": ("clip.mp4", source.read_bytes(), "video/mp4")})
    assert created.status_code == 201
    asset_id = created.json()["id"]
    assert created.json()["thumbnail_url"] == f"/thumbnails/{asset_id}"
    first = http.get(created.json()["thumbnail_url"])
    assert first.status_code == 200
    assert first.headers["content-type"].startswith("image/jpeg")
    assert first.content.startswith(b"\xff\xd8")

    add_review_asset(engine, "NS-001")
    with Session(engine) as session:
        uploaded = session.get(Asset, asset_id)
        uploaded.analysis_state = "complete"
        session.add(Activity(text="City launch film submitted for review", by="Alex Morgan"))
        session.commit()
        app_module.seed_database(session)
        assert session.get(Asset, asset_id).analysis_state == "complete"
        assert session.get(Asset, "NS-001") is None
        assert session.query(Activity).count() == 1
    assert http.get(f"/uploads/{asset_id}").status_code == 200
    partial = http.get(f"/uploads/{asset_id}", headers={"Range": "bytes=0-3"})
    assert partial.status_code == 206
    assert len(partial.content) == 4

    thumbnail = (upload_dir / uploaded.file_name).with_suffix(".jpg")
    thumbnail.unlink()
    assert http.get(f"/thumbnails/{asset_id}").content.startswith(b"\xff\xd8")


class FakeAzure:
    def __init__(self, kind: str):
        self.kind = kind
        self.poll_count = 0

    def require_config(self, kind: str):
        assert kind == self.kind

    def submit_image(self, path, mime_type):
        assert path.is_file() and mime_type == "image/png"
        return "https://example.test/result/1"

    def poll_image(self, operation_url):
        assert operation_url == "https://example.test/result/1"
        return "complete", {"result": {"contents": [{"fields": {"tags": {"valueArray": [
            {"valueString": "Coast"}, {"valueString": "Sky"}, {"valueString": "coast"}
        ]}}}]}}

    def stage_audio(self, asset, path):
        assert path.is_file() and asset.type == "video"
        return "video.flac"

    def submit_video(self, asset, audio_blob):
        assert audio_blob == "video.flac"
        return "https://speech.test/speechtotext/transcriptions/job-1"

    def delete_audio(self, name):
        assert name == "video.flac"

    def tag_transcript(self, transcript):
        assert transcript[0]["text"] == "A bright blue ocean"
        return ["Ocean"]

    def poll_video(self, job_url):
        assert job_url.endswith("job-1")
        self.poll_count += 1
        if self.poll_count == 1:
            return "indexing", None
        return "complete", {"recognizedPhrases": [{"recognitionStatus": "Success",
            "offsetInTicks": 12_340_000, "durationInTicks": 20_000_000,
            "nBest": [{"display": "A bright blue ocean"}]}]}


def test_image_analysis_merges_tags_edited_during_processing(client):
    http, engine, upload_dir = client
    created = http.post("/api/upload", headers=as_user("alex"),
                        files={"file": ("coast.png", b"\x89PNG\r\n\x1a\nexample", "image/png")})
    assert created.status_code == 201
    asset_id = created.json()["id"]
    azure = FakeAzure("image")
    assert run_analysis_step(engine, upload_dir, azure)
    assert http.get(f"/api/assets/{asset_id}", headers=as_user("alex")).json()["analysis_state"] == "indexing"
    http.patch(f"/api/assets/{asset_id}", headers=as_user("alex"), json={"tags": ["Editorial"]})
    assert run_analysis_step(engine, upload_dir, azure)
    asset = http.get(f"/api/assets/{asset_id}", headers=as_user("alex")).json()
    assert asset["analysis_state"] == "complete"
    assert asset["tags"] == ["Editorial", "Coast", "Sky"]
    assert asset["ai_tags"] == ["Coast", "Sky"]
    assert asset["analysis_progress"] == 100
    stale_edit = http.patch(f"/api/assets/{asset_id}", headers=as_user("alex"),
                            json={"tag_additions": ["Campaign"], "tag_removals": ["Editorial"]})
    assert stale_edit.json()["tags"] == ["Coast", "Sky", "Campaign"]
    assert asset_id in [item["id"] for item in http.get("/api/assets?q=sky", headers=as_user("alex")).json()]
    http.patch(f"/api/assets/{asset_id}", headers=as_user("alex"), json={"tags": ["Editorial"]})
    assert asset_id not in [item["id"] for item in http.get("/api/assets?q=sky", headers=as_user("alex")).json()]


def test_video_progress_transcript_correction_search_and_permissions(client):
    http, engine, upload_dir = client
    mp4 = b"\x00\x00\x00\x14ftypisomexample"
    created = http.post("/api/upload", headers=as_user("alex"),
                        files={"file": ("clip.mp4", mp4, "video/mp4")})
    asset_id = created.json()["id"]
    azure = FakeAzure("video")
    run_analysis_step(engine, upload_dir, azure)
    run_analysis_step(engine, upload_dir, azure)
    in_progress = http.get(f"/api/assets/{asset_id}", headers=as_user("alex")).json()
    assert in_progress["analysis_state"] == "indexing"
    assert in_progress["analysis_progress"] is None
    run_analysis_step(engine, upload_dir, azure)
    run_analysis_step(engine, upload_dir, azure)
    finished = http.get(f"/api/assets/{asset_id}", headers=as_user("alex")).json()
    assert finished["transcript"] == [{"id": 1, "start": "0:00:01.234", "end": "0:00:03.234", "text": "A bright blue ocean"}]
    assert finished["ai_tags"] == ["Ocean"]
    corrected = http.patch(f"/api/assets/{asset_id}/transcript/1", headers=as_user("alex"),
                           json={"text": "A calm blue sea"})
    assert corrected.status_code == 200
    assert corrected.json()["transcript"][0]["text"] == "A calm blue sea"
    assert asset_id in [item["id"] for item in http.get("/api/assets?q=calm", headers=as_user("alex")).json()]
    assert asset_id not in [item["id"] for item in http.get("/api/assets?q=bright", headers=as_user("alex")).json()]
    assert http.patch(f"/api/assets/{asset_id}/transcript/1", headers=as_user("quinn"),
                      json={"text": "changed"}).status_code == 404


def test_tag_failure_keeps_transcript_and_tag_retry_skips_speech(client):
    http, engine, upload_dir = client
    created = http.post("/api/upload", headers=as_user("alex"),
                        files={"file": ("clip.mp4", b"\0\0\0\x14ftypisomexample", "video/mp4")})
    asset_id = created.json()["id"]

    class FailTags(FakeAzure):
        def tag_transcript(self, transcript):
            raise AnalysisFailure("private model error")

    azure = FailTags("video")
    for _ in range(4):
        run_analysis_step(engine, upload_dir, azure)
    detail = http.get(f"/api/assets/{asset_id}", headers=as_user("alex")).json()
    assert detail["analysis_state"] == "complete"
    assert detail["transcript"][0]["text"] == "A bright blue ocean"
    assert "Retry tags" in detail["tag_error"]
    assert "private model error" not in detail["tag_error"]
    assert http.post(f"/api/assets/{asset_id}/tags/retry", headers=as_user("quinn")).status_code == 404
    assert http.post(f"/api/assets/{asset_id}/tags/retry", headers=as_user("alex")).json()["analysis_state"] == "preparing"

    class TagsOnly(FakeAzure):
        def submit_video(self, asset, audio_blob):
            raise AssertionError("Speech must not be charged again")

        def poll_video(self, job_url):
            raise AssertionError("Speech must not be polled again")

    run_analysis_step(engine, upload_dir, TagsOnly("video"))
    detail = http.get(f"/api/assets/{asset_id}", headers=as_user("alex")).json()
    assert detail["tag_error"] is None
    assert detail["ai_tags"] == ["Ocean"]


def test_video_without_audio_completes_without_speech(client):
    http, engine, upload_dir = client
    created = http.post("/api/upload", headers=as_user("alex"),
                        files={"file": ("silent.mp4", b"\0\0\0\x14ftypisomexample", "video/mp4")})
    asset_id = created.json()["id"]

    class NoAudio(FakeAzure):
        def stage_audio(self, asset, path):
            return None

        def submit_video(self, asset, audio_blob):
            raise AssertionError("No Speech job expected")

    run_analysis_step(engine, upload_dir, NoAudio("video"))
    detail = http.get(f"/api/assets/{asset_id}", headers=as_user("alex")).json()
    assert detail["analysis_state"] == "complete"
    assert detail["transcript"] == []
    assert detail["tag_error"] is None


def test_failed_analysis_can_retry_after_configuration(client):
    http, engine, upload_dir = client
    created = http.post("/api/upload", headers=as_user("alex"),
                        files={"file": ("image.png", b"\x89PNG\r\n\x1a\nexample", "image/png")})
    asset_id = created.json()["id"]

    class NoConfig(FakeAzure):
        def require_config(self, kind):
            raise AnalysisFailure("Azure Content Understanding is not configured")

    run_analysis_step(engine, upload_dir, NoConfig("image"))
    failed = http.get(f"/api/assets/{asset_id}", headers=as_user("alex")).json()
    assert failed["analysis_state"] == "failed"
    assert "not configured" in failed["analysis_error"]
    assert http.post(f"/api/assets/{asset_id}/analysis/retry", headers=as_user("quinn")).status_code == 404
    retried = http.post(f"/api/assets/{asset_id}/analysis/retry", headers=as_user("alex"))
    assert retried.json()["analysis_state"] == "queued"
    run_analysis_step(engine, upload_dir, FakeAzure("image"))
    run_analysis_step(engine, upload_dir, FakeAzure("image"))
    assert http.get(f"/api/assets/{asset_id}", headers=as_user("alex")).json()["analysis_state"] == "complete"


def test_retry_resumes_speech_job_after_temporary_poll_error(client):
    from atlas_backend.analysis import SpeechJobFailure

    http, engine, upload_dir = client
    created = http.post("/api/upload", headers=as_user("alex"),
                        files={"file": ("clip.mp4", b"\0\0\0\x14ftypisomexample", "video/mp4")})
    asset_id = created.json()["id"]
    run_analysis_step(engine, upload_dir, FakeAzure("video"))

    class TemporaryPoll(FakeAzure):
        def poll_video(self, job_url):
            raise AnalysisFailure("Speech polling was temporarily unavailable")

    run_analysis_step(engine, upload_dir, TemporaryPoll("video"))
    retried = http.post(f"/api/assets/{asset_id}/analysis/retry", headers=as_user("alex")).json()
    assert retried["analysis_state"] == "indexing"

    class Resume(FakeAzure):
        def submit_video(self, asset, audio_blob):
            raise AssertionError("The existing Speech job must be reused")

        def poll_video(self, job_url):
            return "complete", {"recognizedPhrases": []}

    run_analysis_step(engine, upload_dir, Resume("video"))
    assert http.get(f"/api/assets/{asset_id}", headers=as_user("alex")).json()["analysis_state"] == "preparing"

    created = http.post("/api/upload", headers=as_user("alex"),
                        files={"file": ("other.mp4", b"\0\0\0\x14ftypisomexample", "video/mp4")})
    failed_id = created.json()["id"]
    run_analysis_step(engine, upload_dir, FakeAzure("video"), failed_id)

    class TerminalPoll(FakeAzure):
        def poll_video(self, job_url):
            raise SpeechJobFailure("Azure Speech batch transcription failed")

    run_analysis_step(engine, upload_dir, TerminalPoll("video"), failed_id)
    assert http.post(f"/api/assets/{failed_id}/analysis/retry", headers=as_user("alex")).json()["analysis_state"] == "queued"


def test_speech_permission_failure_is_safe(client):
    from atlas_backend import analysis

    http, engine, upload_dir = client
    created = http.post("/api/upload", headers=as_user("alex"),
                        files={"file": ("clip.mp4", b"\x00\x00\x00\x14ftypisomexample", "video/mp4")})
    asset_id = created.json()["id"]

    class DeniedAzure(FakeAzure):
        def submit_video(self, asset, audio_blob):
            request = analysis.httpx.Request("POST", "https://speech.test/speechtotext/transcriptions:submit")
            response = analysis.httpx.Response(403, request=request, json={"error": {
                "code": "AuthorizationFailed", "message": "Client private-identifier is not authorized",
            }})
            response.raise_for_status()

    run_analysis_step(engine, upload_dir, DeniedAzure("video"))
    asset = http.get(f"/api/assets/{asset_id}", headers=as_user("alex")).json()
    assert asset["analysis_state"] == "failed"
    assert "403" in asset["analysis_error"]
    assert "private-identifier" not in asset["analysis_error"]


def test_speech_submit_400_keeps_safe_provider_code(client):
    from atlas_backend import analysis

    http, engine, upload_dir = client
    created = http.post("/api/upload", headers=as_user("alex"),
                        files={"file": ("clip.mp4", b"\x00\x00\x00\x14ftypisomexample", "video/mp4")})
    asset_id = created.json()["id"]

    class BadRequestAzure(FakeAzure):
        def submit_video(self, asset, audio_blob):
            request = analysis.httpx.Request("POST", "https://speech.test/speechtotext/transcriptions:submit")
            response = analysis.httpx.Response(400, request=request, json={
                "code": "InvalidRequest", "innerError": {"code": "InaccessibleCustomerStorage"},
                "message": "Private URL and token must not be shown",
            })
            response.raise_for_status()

    run_analysis_step(engine, upload_dir, BadRequestAzure("video"))
    asset = http.get(f"/api/assets/{asset_id}", headers=as_user("alex")).json()
    assert asset["analysis_state"] == "failed"
    assert asset["analysis_error"] == ("Azure Speech submission failed "
                                       "(400, InvalidRequest: InaccessibleCustomerStorage)")
    assert "Private URL" not in asset["analysis_error"]


def test_new_upload_formats_and_limits(client, monkeypatch):
    http, _, _ = client
    assert http.post("/api/upload", headers=as_user("alex"),
                     files={"file": ("a.webp", b"RIFFxxxxWEBP", "image/webp")}).status_code == 400
    assert http.post("/api/upload", headers=as_user("alex"),
                     files={"file": ("a.png", b"not a png", "image/png")}).status_code == 400
    monkeypatch.setattr(app_module, "MAX_VIDEO_BYTES", 12)
    assert http.post("/api/upload", headers=as_user("alex"),
                     files={"file": ("a.mp4", b"\0\0\0\x14ftypisomexample", "video/mp4")}).status_code == 413


def test_resume_existing_video_job_with_no_speech(client):
    http, engine, upload_dir = client
    created = http.post("/api/upload", headers=as_user("alex"),
                        files={"file": ("silent.mp4", b"\0\0\0\x14ftypisomexample", "video/mp4")})
    asset_id = created.json()["id"]
    with Session(engine) as db:
        asset = db.get(Asset, asset_id)
        asset.analysis_state = "indexing"
        asset.speech_job_url = "https://speech.test/speechtotext/transcriptions/job-1"
        db.commit()

    class SilentAzure(FakeAzure):
        def poll_video(self, job_url):
            return "complete", {"recognizedPhrases": []}

        def submit_video(self, asset, audio_blob):
            raise AssertionError("A resumed job must not upload the video again")

        def tag_transcript(self, transcript):
            assert transcript == []
            return []

    run_analysis_step(engine, upload_dir, SilentAzure("video"))
    run_analysis_step(engine, upload_dir, SilentAzure("video"))
    asset = http.get(f"/api/assets/{asset_id}", headers=as_user("alex")).json()
    assert asset["analysis_state"] == "complete"
    assert asset["transcript"] == []


def test_audio_extraction_preserves_timeline_and_original(tmp_path, monkeypatch):
    from atlas_backend import analysis

    source = tmp_path / "source.mp4"
    source.write_bytes(b"original-video-bytes")
    output = tmp_path / "audio.flac"
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"streams": [
                {"codec_type": "video"}, {"codec_type": "audio"},
            ]}), stderr="")
        Path(command[-1]).write_bytes(b"extracted-audio")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(analysis.subprocess, "run", fake_run)
    assert analysis.extract_audio(source, output)
    assert [item[0] for item in commands] == ["ffprobe", "ffmpeg"]
    assert "aresample=async=1:first_pts=0" in commands[1]
    assert source.read_bytes() == b"original-video-bytes"
