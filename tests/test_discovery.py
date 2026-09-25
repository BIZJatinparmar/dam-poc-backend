"""Discovery index and retrieval behavior. Set ATLAS_TEST_DATABASE_URL for PostgreSQL checks."""

from datetime import date
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import os

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from atlas_backend.database import Base
from atlas_backend.discovery import (build_chunks, mark_search_pending, run_search_index_step,
                                     search_hits, source_hash, sync_search_states)
from atlas_backend.media_access import signed_media_url, verify_media_url
from atlas_backend.models import Asset, SearchChunk, SearchState, User


def asset(asset_id: str, kind: str, tags=None, transcript=None) -> Asset:
    return Asset(id=asset_id, title="Summer campaign", type=kind, campaign="Launch", brand="Northstar",
                 status="approved", rights=date(2029, 1, 1), audience="external", tags=tags or [],
                 ai_tags=[], description="A campaign asset", owner_id=None, owner="Alex", size="1 MB",
                 uploaded="24 Sep 2026", art="upload", transcript=transcript or [], analysis_state="complete")


def test_chunks_preserve_moments_and_image_tags_only():
    video = asset("video", "video", ["coast"], [
        {"id": 1, "start": "0:00:01.200", "end": "0:00:03.000", "text": "Blue ocean"},
        {"id": 2, "start": "0:01:10.000", "end": "0:01:12.000", "text": "Ocean launch"},
    ])
    chunks = build_chunks(video)
    assert [chunk["kind"] for chunk in chunks] == ["video_metadata", "transcript", "transcript"]
    assert [chunk["start_seconds"] for chunk in chunks[1:]] == [1.2, 70.0]
    image = asset("image", "image", ["forest"])
    assert build_chunks(image)[0]["content"] == "Tags: forest"
    before = source_hash(image)
    image.title = "Changed but not searchable"
    assert source_hash(image) == before


def test_chunks_use_active_tags_after_generated_tag_is_corrected():
    image = asset("image", "image", ["shore"])
    image.ai_tags = ["coast"]
    assert build_chunks(image)[0]["content"] == "Tags: shore"
    assert source_hash(image) == source_hash(asset("image", "image", ["shore"]))

    video = asset("video", "video", ["shore"])
    video.ai_tags = ["coast"]
    metadata = build_chunks(video)[0]["content"]
    assert "shore" in metadata
    assert "coast" not in metadata


def test_media_links_are_scoped_and_expire(monkeypatch):
    from atlas_backend import media_access

    monkeypatch.setattr(media_access.time, "time", lambda: 1000)
    media = asset("video", "video")
    media.file_name = "video.mp4"
    viewer = User(id="agency", name="Agency", email="agency@example.com", role="agency",
                  team="Agency", active=True, initials="AG")
    url = signed_media_url(media, viewer, "original")
    params = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
    assert verify_media_url(media.id, "original", params["viewer"], int(params["expires"]),
                            params["signature"]) == viewer.id
    with pytest.raises(HTTPException):
        verify_media_url(media.id, "thumbnail", params["viewer"], int(params["expires"]), params["signature"])
    monkeypatch.setattr(media_access.time, "time", lambda: 5000)
    with pytest.raises(HTTPException):
        verify_media_url(media.id, "original", params["viewer"], int(params["expires"]), params["signature"])


def test_search_keeps_nearby_wording_and_rejects_weak_candidates():
    chunk = SimpleNamespace(id=7, asset_id="video", kind="transcript", content="A project overview",
                            start_seconds=1.0, end_seconds=3.0)
    video = SimpleNamespace(id="video")

    class SearchDb:
        def execute(self, statement, params):
            sql = str(statement)
            if "c.embedding <=>" in sql:
                score = {"user explaining project": 0.31, "user explaining projects": 0.29,
                         "unrelated nonsense": 0.05}[params["query"]]
                return SimpleNamespace(all=lambda: [SimpleNamespace(id=7, score=score)])
            return SimpleNamespace(all=lambda: [])

        def scalars(self, statement):
            entity = statement.column_descriptions[0]["entity"]
            return [chunk] if entity is SearchChunk else [video] if entity is Asset else []

    user = SimpleNamespace(role="admin")
    embed = lambda _: ["[0]"]
    for query in ("user explaining project", "user explaining projects"):
        assert [item.id for item, _ in search_hits(SearchDb(), user, query, embedder=embed)] == ["video"]
    assert search_hits(SearchDb(), user, "unrelated nonsense", embedder=embed) == []


def test_search_uses_partial_keyword_coverage_without_returning_single_weak_words():
    chunks = [SimpleNamespace(id=index, asset_id=f"video-{index}", kind="transcript",
                              content="Project discussion", start_seconds=1.0, end_seconds=3.0)
              for index in (1, 2)]
    assets = [SimpleNamespace(id=f"video-{index}") for index in (1, 2)]

    class SearchDb:
        def execute(self, statement, params):
            sql = str(statement)
            if "c.embedding <=>" in sql:
                return SimpleNamespace(all=lambda: [SimpleNamespace(id=index, score=0.05) for index in (1, 2)])
            assert "to_tsquery('english', :keyword_query)" in sql
            assert params["keyword_query"] == "user | explaining | projects"
            return SimpleNamespace(all=lambda: [SimpleNamespace(id=1, score=0.2, coverage=2),
                                                SimpleNamespace(id=2, score=0.1, coverage=1)])

        def scalars(self, statement):
            return chunks if statement.column_descriptions[0]["entity"] is SearchChunk else assets

    hits = search_hits(SearchDb(), SimpleNamespace(role="admin"), "user explaining projects",
                       embedder=lambda _: ["[0]"])
    assert [item.id for item, _ in hits] == ["video-1"]


@pytest.mark.skipif(not os.getenv("ATLAS_TEST_DATABASE_URL"), reason="PostgreSQL integration database not configured")
def test_pgvector_backfill_search_stale_index_and_retry(monkeypatch):
    url = os.environ["ATLAS_TEST_DATABASE_URL"]
    assert make_url(url).database.endswith("_test"), "Use a dedicated _test database"
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    def embed(values):
        vectors = []
        for value in values:
            lower = value.lower()
            axis = 0 if "ocean" in lower or "sea" in lower else 1 if "forest" in lower else 2
            components = [0.0] * 1536
            components[axis] = 1.0
            vectors.append("[" + ",".join(str(item) for item in components) + "]")
        return vectors

    try:
        with Session(engine) as db:
            admin = User(id="admin", name="Admin", email="admin@example.com", role="admin",
                         team="DAM", active=True, initials="AD")
            agency = User(id="agency", name="Agency", email="agency@example.com", role="agency",
                          team="External", active=True, initials="AG")
            video = asset("video", "video", ["launch"], [
                {"id": 1, "start": "0:00:01.000", "end": "0:00:03.000", "text": "Ocean waves"},
                {"id": 2, "start": "0:01:10.000", "end": "0:01:13.000", "text": "The ocean is calm"},
            ])
            image = asset("image", "image", ["forest"])
            image.status = "draft"
            db.add_all([admin, agency, video, image])
            db.commit()
        sync_search_states(engine)
        assert run_search_index_step(engine, embed)
        assert run_search_index_step(engine, embed)
        with Session(engine) as db:
            admin, agency = db.get(User, "admin"), db.get(User, "agency")
            hits = search_hits(db, admin, "sea", embedder=embed)
            assert hits[0][0].id == "video"
            assert sorted(match["start_seconds"] for match in hits[0][1] if match["start_seconds"] is not None) == [1.0, 70.0]
            weak_components = [0.0] * 1536
            weak_components[3] = 1.0
            weak_query_vector = "[" + ",".join(str(item) for item in weak_components) + "]"
            assert search_hits(db, admin, "ocean waves projects",
                               embedder=lambda _: [weak_query_vector])[0][0].id == "video"
            assert search_hits(db, admin, "oceans waves projects",
                               embedder=lambda _: [weak_query_vector])[0][0].id == "video"
            assert not search_hits(db, admin, "waves projects nonsense",
                                   embedder=lambda _: [weak_query_vector])
            assert search_hits(db, admin, "forest", media_type="image", embedder=embed)[0][0].id == "image"
            assert not search_hits(db, agency, "forest", embedder=embed)
        from fastapi.testclient import TestClient
        from atlas_backend import app as app_module
        from atlas_backend.database import get_db

        def test_db():
            with Session(engine) as session:
                yield session

        original_search = app_module.search_hits
        monkeypatch.setattr(app_module, "search_hits", lambda db, user, query, media_type, status:
                            original_search(db, user, query, media_type, status, embedder=embed))
        monkeypatch.setattr(app_module, "grounded_answer", lambda message, history, sources:
                            ("The video discusses the ocean.", [sources[0]["id"]]) if sources else ("No evidence.", []))
        monkeypatch.setattr(app_module, "engine", engine)
        monkeypatch.setattr(app_module, "ANALYSIS_WORKER_ENABLED", False)
        app_module.app.dependency_overrides[get_db] = test_db
        try:
            with TestClient(app_module.app) as client:
                response = client.post("/api/discovery/search", headers={"X-Demo-User": "admin"},
                                       json={"query": "sea", "media_type": "video"})
                assert response.status_code == 200
                assert response.json()[0]["matches"][0]["start_seconds"] in (1.0, 70.0)
                chat = client.post("/api/discovery/chat", headers={"X-Demo-User": "admin"},
                                   json={"message": "What was said?", "history": [
                                       {"role": "user", "content": "Tell me about the sea"},
                                       {"role": "assistant", "content": "The video mentions it."}]})
                assert chat.status_code == 200
                assert chat.json()["citations"][0]["asset"]["id"] == "video"
                hidden = client.post("/api/discovery/search", headers={"X-Demo-User": "agency"},
                                     json={"query": "forest", "media_type": "image"})
                assert hidden.json() == []
        finally:
            app_module.app.dependency_overrides.clear()
        with Session(engine) as db:
            video = db.get(Asset, "video")
            video.transcript = [{"id": 1, "start": "0:00:01.000", "end": "0:00:03.000", "text": "Forest scene"}]
            mark_search_pending(db, video)
            db.commit()
            assert not search_hits(db, admin, "sea", media_type="video", embedder=embed)

        def fail(_values):
            raise RuntimeError("offline")

        assert run_search_index_step(engine, fail)
        with Session(engine) as db:
            state = db.get(SearchState, "video")
            assert state.status == "failed"
            with pytest.raises(HTTPException):
                app_module.retry_failed_search_indexes(db, db.get(User, "agency"))
            assert app_module.retry_failed_search_indexes(db, db.get(User, "admin"))["queued"] == 1
            assert state.status == "pending"
        assert run_search_index_step(engine, embed)
        with Session(engine) as db:
            assert search_hits(db, db.get(User, "admin"), "forest", media_type="video", embedder=embed)[0][0].id == "video"
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
