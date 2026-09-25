"""FastAPI application for the Atlas DAM demo.

The X-Demo-User header is a UI switcher, not authentication. Replace it with
Microsoft Entra ID before using this application with real assets or users.
"""

from __future__ import annotations

import re
import asyncio
import os
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated
from uuid import uuid4

from azure.core.exceptions import AzureError
from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .analysis import TAG_LOCK, analysis_worker, clean_tags
from .database import engine, get_db
from .demo_data import USERS, art_svg
from .discovery import grounded_answer, mark_search_pending, search_hits, search_worker
from .media_access import signed_media_url, verify_media_url
from .models import Activity, Asset, SearchState, User, utc_now
from .schemas import (ActivityRead, AssetDetail, AssetRead, AssetUpdate, DiscoveryChatRequest,
                      DiscoveryChatResponse, DiscoverySearchRequest, DiscoveryResult, Summary,
                      TranscriptUpdate, UserCreate, UserRead, UserUpdate)
from .storage import create_storage
from .video_metadata import duration_label, probe_video_duration


MAX_IMAGE_BYTES = 12_000_000
MAX_VIDEO_BYTES = 100_000_000
ANALYSIS_WORKER_ENABLED = os.getenv("ATLAS_ANALYSIS_WORKER", "1") == "1"
ALLOWED_MIME = {
    "image/jpeg": ".jpg", "image/png": ".png", "video/mp4": ".mp4",
}
TRANSITIONS = {
    "draft": {"in_review"},
    "in_review": {"draft", "approved"},
    "approved": {"draft", "published"},
    "published": {"approved"},
}
STARTER_ASSET_IDS = tuple(f"NS-{number:03d}" for number in range(1, 9))
STARTER_ACTIVITY = (
    "Coastal campaign hero published to the business library",
    "City launch film submitted for review",
    "Forest textures approved",
)


def seed_database(db: Session) -> None:
    if db.scalar(select(User.id).limit(1)) is None:
        db.add_all(User(**item) for item in USERS)
        db.flush()
    db.execute(delete(Asset).where(Asset.id.in_(STARTER_ASSET_IDS), Asset.file_name.is_(None)))
    db.execute(delete(Activity).where(Activity.text.in_(STARTER_ACTIVITY)))
    for asset in db.scalars(select(Asset).where(Asset.type == "video", Asset.analysis_state == "complete",
                                               Asset.video_category.is_(None), Asset.classification_error.is_(None),
                                               Asset.category_source.is_(None))):
        if asset.transcript:
            asset.analysis_state = "preparing"
    db.commit()


def backfill_video_sizes(db: Session) -> None:
    """Read object metadata for videos uploaded before exact sizes were stored."""
    for asset in db.scalars(select(Asset).where(Asset.type == "video", Asset.size_bytes.is_(None),
                                               Asset.file_name.is_not(None))):
        try:
            asset.size_bytes = storage.size_bytes(asset.file_name)
        except (OSError, AzureError):
            continue
    db.commit()


def backfill_video_durations(db: Session) -> None:
    """Probe originals uploaded before video duration was recorded."""
    for asset in db.scalars(select(Asset).where(Asset.type == "video", Asset.duration_seconds.is_(None),
                                               Asset.file_name.is_not(None))):
        try:
            with storage.materialize(asset.file_name) as path:
                seconds = probe_video_duration(path)
        except (OSError, AzureError):
            continue
        if seconds is not None:
            asset.duration_seconds = seconds
            asset.duration = duration_label(seconds)
    db.commit()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    with Session(engine) as db:
        seed_database(db)
        backfill_video_sizes(db)
        backfill_video_durations(db)
    worker = asyncio.create_task(analysis_worker(engine, storage)) if ANALYSIS_WORKER_ENABLED else None
    discovery_worker = asyncio.create_task(search_worker(engine)) if ANALYSIS_WORKER_ENABLED else None
    yield
    for task in (worker, discovery_worker):
        if not task:
            continue
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Atlas DAM demo API", version="0.2.0", lifespan=lifespan)
storage = create_storage()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip().rstrip("/") for origin in os.getenv("ATLAS_ALLOWED_ORIGINS", "http://127.0.0.1:5173").split(",") if origin.strip()],
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Content-Type", "X-Demo-User"],
)


SessionDep = Annotated[Session, Depends(get_db)]


@app.get("/")
def root():
    return {"service": "atlas-api", "status": "ok"}


def current_user(db: SessionDep, demo_user_id: Annotated[str, Header(alias="X-Demo-User")] = "maya") -> User:
    user = db.get(User, demo_user_id)
    if not user or not user.active:
        raise HTTPException(403, "Demo user is inactive or unknown")
    return user


UserDep = Annotated[User, Depends(current_user)]


def visible(asset: Asset, user: User) -> bool:
    return user.role != "agency" or (
        asset.audience == "external"
        and asset.status in {"approved", "published"}
        and asset.rights >= date.today()
    )


def public_asset(asset: Asset, user: User, detail: bool = False) -> AssetRead | AssetDetail:
    model = AssetDetail if detail else AssetRead
    data = model.model_validate(asset)
    if asset.file_name:
        data.url = signed_media_url(asset, user, "original")
        if asset.type == "video":
            data.thumbnail_url = signed_media_url(asset, user, "thumbnail")
    return data


def require_admin(user: User) -> None:
    if user.role != "admin":
        raise HTTPException(403, "Only admins can manage users or approve content")


def search_score(asset: Asset, query: str) -> int:
    synonyms = {
        "summer": ["sunlit", "warm"], "outdoor": ["outdoors", "nature", "landscape"],
        "people": ["friends", "team"], "beach": ["coast", "ocean"], "film": ["video"],
    }
    text = " ".join((asset.title, asset.description, asset.campaign, asset.type, *asset.tags,
                     *(segment.get("text", "") for segment in asset.transcript or []))).lower()
    return sum(term in text or any(word in text for word in synonyms.get(term, [])) for term in re.findall(r"[a-z0-9]+", query.lower()))


def log_activity(db: Session, text: str, user: User) -> None:
    db.add(Activity(text=text, by=user.name))


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/users", response_model=list[UserRead])
def list_users(db: SessionDep, user: UserDep):
    if user.role != "admin":
        return [user]
    return db.scalars(select(User).order_by(User.name)).all()


@app.post("/api/users", response_model=UserRead, status_code=201)
def create_user(payload: UserCreate, db: SessionDep, user: UserDep):
    require_admin(user)
    name = payload.name.strip()
    if not name:
        raise HTTPException(422, "Name cannot be blank")
    email = str(payload.email).lower()
    new_user = User(
        id=f"user-{uuid4().hex[:12]}", name=name, email=email, role=payload.role,
        team="External agency" if payload.role == "agency" else "Marketing",
        active=True, initials="".join(part[0] for part in name.split()[:2]).upper(),
    )
    db.add(new_user)
    log_activity(db, f"{name} added as {payload.role}", user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "This email already exists") from None
    db.refresh(new_user)
    return new_user


@app.patch("/api/users/{user_id}", response_model=UserRead)
def update_user(user_id: str, payload: UserUpdate, db: SessionDep, user: UserDep):
    require_admin(user)
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, "User not found")
    if target.id == user.id and not payload.active:
        raise HTTPException(400, "You cannot disable yourself")
    target.active = payload.active
    log_activity(db, f"{target.name} {'enabled' if target.active else 'disabled'}", user)
    db.commit()
    db.refresh(target)
    return target


@app.get("/api/activity", response_model=list[ActivityRead])
def list_activity(db: SessionDep, user: UserDep):
    if user.role == "agency":
        return []
    return db.scalars(select(Activity).order_by(Activity.id.desc()).limit(8)).all()


@app.get("/api/assets", response_model=list[AssetRead])
def list_assets(
    db: SessionDep, user: UserDep, q: str = "", status: str = "all",
    type: str = Query(default="all"),
):
    assets = [item for item in db.scalars(select(Asset).order_by(Asset.created_at.desc(), Asset.id.desc())) if visible(item, user)]
    if status != "all":
        assets = [item for item in assets if item.status == status]
    if type != "all":
        assets = [item for item in assets if item.type == type]
    if q.strip():
        assets = [(search_score(item, q), item) for item in assets]
        assets = [item for rank, item in sorted(assets, key=lambda pair: pair[0], reverse=True) if rank]
    return [public_asset(asset, user) for asset in assets]


@app.post("/api/discovery/search", response_model=list[DiscoveryResult])
def discovery_search(payload: DiscoverySearchRequest, db: SessionDep, user: UserDep):
    try:
        hits = search_hits(db, user, payload.query, payload.media_type, payload.status)
    except Exception:
        raise HTTPException(503, "Semantic search is temporarily unavailable") from None
    return [{"asset": public_asset(asset, user), "matches": matches} for asset, matches in hits]


@app.post("/api/discovery/chat", response_model=DiscoveryChatResponse)
def discovery_chat(payload: DiscoveryChatRequest, db: SessionDep, user: UserDep):
    prior_questions = [item.content for item in payload.history if item.role == "user"][-2:]
    retrieval_query = " ".join([*prior_questions, payload.message])
    try:
        hits = search_hits(db, user, retrieval_query, payload.media_type, payload.status)
        sources = [{**match, "title": asset.title} for asset, matches in hits for match in matches][:12]
        answer, citation_ids = grounded_answer(payload.message,
                                                [item.model_dump() for item in payload.history], sources)
    except Exception:
        raise HTTPException(503, "Discovery chat is temporarily unavailable") from None
    by_id = {match["id"]: (asset, match) for asset, matches in hits for match in matches}
    return {"answer": answer, "citations": [
        {"asset": public_asset(by_id[chunk_id][0], user), "match": by_id[chunk_id][1]}
        for chunk_id in citation_ids if chunk_id in by_id]}


@app.get("/api/discovery/index-status")
def discovery_index_status(db: SessionDep, user: UserDep):
    states = db.execute(select(SearchState, Asset).join(Asset, Asset.id == SearchState.asset_id)).all()
    return [{"asset_id": asset.id, "title": asset.title, "status": state.status, "error": state.error}
            for state, asset in states if visible(asset, user) and state.status != "ready"]


@app.post("/api/assets/{asset_id}/search/retry")
def retry_search_index(asset_id: str, db: SessionDep, user: UserDep):
    asset = db.get(Asset, asset_id)
    if not asset or not visible(asset, user):
        raise HTTPException(404, "Asset not found")
    if user.role == "agency":
        raise HTTPException(403, "Agency users have read-only access")
    state = db.get(SearchState, asset_id)
    if not state or state.status != "failed":
        raise HTTPException(409, "Search indexing has not failed")
    state.status = "pending"
    state.error = None
    state.updated_at = utc_now()
    db.commit()
    return {"asset_id": asset_id, "status": "pending"}


@app.post("/api/discovery/retry-failed")
def retry_failed_search_indexes(db: SessionDep, user: UserDep):
    require_admin(user)
    result = db.execute(update(SearchState).where(SearchState.status == "failed")
                        .values(status="pending", error=None, updated_at=utc_now()))
    db.commit()
    return {"queued": result.rowcount}


@app.get("/api/summary", response_model=Summary)
def get_summary(db: SessionDep, user: UserDep):
    assets = [item for item in db.scalars(select(Asset)) if visible(item, user)]
    return Summary(
        total=len(assets), images=sum(item.type == "image" for item in assets),
        videos=sum(item.type == "video" for item in assets),
        in_review=sum(item.status == "in_review" for item in assets),
        published=sum(item.status == "published" for item in assets),
    )


@app.get("/api/assets/{asset_id}", response_model=AssetDetail)
def get_asset(asset_id: str, db: SessionDep, user: UserDep):
    asset = db.get(Asset, asset_id)
    if not asset or not visible(asset, user):
        raise HTTPException(404, "Asset not found")
    return public_asset(asset, user, detail=True)


@app.patch("/api/assets/{asset_id}", response_model=AssetRead)
def update_asset(asset_id: str, payload: AssetUpdate, db: SessionDep, user: UserDep):
    asset = db.get(Asset, asset_id)
    if not asset or not visible(asset, user):
        raise HTTPException(404, "Asset not found")
    if user.role == "agency":
        raise HTTPException(403, "Agency users have read-only access")
    updates = payload.model_dump(exclude_unset=True)
    if any(value is None for value in updates.values()):
        raise HTTPException(422, "Fields cannot be null")
    if "tags" in updates and ("tag_additions" in updates or "tag_removals" in updates):
        raise HTTPException(422, "Use either tags or tag changes")
    if ("video_category" in updates or "video_format" in updates) and asset.type != "video":
        raise HTTPException(422, "Only videos can be classified")
    if "video_format" in updates and not (updates.get("video_category") or asset.video_category):
        raise HTTPException(422, "Choose a video topic before setting its format")
    new_status = updates.get("status")
    if new_status:
        if new_status not in TRANSITIONS[asset.status]:
            raise HTTPException(400, "That status change is not allowed")
        if new_status in {"approved", "published"}:
            require_admin(user)
        if new_status == "published" and updates.get("rights", asset.rights) < date.today():
            raise HTTPException(400, "Usage rights have expired")
    with TAG_LOCK:
        db.refresh(asset)
        additions = updates.pop("tag_additions", [])
        removals = {tag.casefold() for tag in updates.pop("tag_removals", [])}
        next_tags = None
        if additions or removals:
            next_tags = clean_tags([tag for tag in asset.tags if tag.casefold() not in removals] + additions,
                                   limit=21)
            if len(next_tags) > 20:
                raise HTTPException(422, "An asset can have up to 20 tags")
        for key, value in updates.items():
            setattr(asset, key, clean_tags(value) if key == "tags" else value)
        if "video_category" in updates or "video_format" in updates:
            asset.category_source = "manual"
            asset.classification_evidence = None
            asset.classification_error = None
        if next_tags is not None:
            asset.tags = next_tags
        mark_search_pending(db, asset)
        log_activity(db, f"{asset.title} updated to {asset.status.replace('_', ' ')}", user)
        db.commit()
    db.refresh(asset)
    return public_asset(asset, user)


@app.patch("/api/assets/{asset_id}/transcript/{segment_id}", response_model=AssetDetail)
def update_transcript(asset_id: str, segment_id: int, payload: TranscriptUpdate, db: SessionDep, user: UserDep):
    asset = db.get(Asset, asset_id)
    if not asset or not visible(asset, user):
        raise HTTPException(404, "Asset not found")
    if user.role == "agency":
        raise HTTPException(403, "Agency users have read-only access")
    if asset.type != "video" or asset.analysis_state != "complete":
        raise HTTPException(409, "Transcript is not ready")
    new_text = payload.text.strip()
    if not new_text:
        raise HTTPException(422, "Transcript text cannot be blank")
    segments = [dict(segment) for segment in asset.transcript]
    for segment in segments:
        if segment["id"] == segment_id:
            segment["text"] = new_text
            break
    else:
        raise HTTPException(404, "Transcript segment not found")
    asset.transcript = segments
    if asset.category_source != "manual":
        asset.video_category = None
        asset.video_format = None
        asset.classification_evidence = None
        asset.category_source = None
    asset.classification_error = None
    asset.analysis_state = "preparing"
    asset.analysis_progress = None
    mark_search_pending(db, asset)
    log_activity(db, f"{asset.title} transcript corrected", user)
    db.commit()
    db.refresh(asset)
    return public_asset(asset, user, detail=True)


@app.post("/api/assets/{asset_id}/analysis/retry", response_model=AssetRead)
def retry_analysis(asset_id: str, db: SessionDep, user: UserDep):
    asset = db.get(Asset, asset_id)
    if not asset or not visible(asset, user):
        raise HTTPException(404, "Asset not found")
    if user.role == "agency":
        raise HTTPException(403, "Agency users have read-only access")
    if asset.analysis_state != "failed":
        raise HTTPException(409, "Only failed analysis can be retried")
    asset.analysis_state = "indexing" if asset.type == "video" and asset.speech_job_url else "queued"
    asset.analysis_progress = None
    asset.analysis_error = None
    db.commit()
    db.refresh(asset)
    return public_asset(asset, user)


@app.post("/api/assets/{asset_id}/tags/retry", response_model=AssetRead)
def retry_tags(asset_id: str, db: SessionDep, user: UserDep):
    asset = db.get(Asset, asset_id)
    if not asset or not visible(asset, user):
        raise HTTPException(404, "Asset not found")
    if user.role == "agency":
        raise HTTPException(403, "Agency users have read-only access")
    if (asset.type != "video" or asset.analysis_state != "complete" or not asset.transcript
            or (not asset.tag_error and asset.ai_tags and not asset.classification_error
                and asset.video_category)):
        raise HTTPException(409, "Video insights are not ready to retry")
    asset.tag_error = None
    asset.classification_error = None
    asset.analysis_state = "preparing"
    asset.analysis_progress = None
    db.commit()
    db.refresh(asset)
    return public_asset(asset, user)


@app.post("/api/videos/classification/retry-failed")
def retry_failed_video_classifications(db: SessionDep, user: UserDep):
    if user.role == "agency":
        raise HTTPException(403, "Agency users have read-only access")
    queued = 0
    for asset in db.scalars(select(Asset).where(Asset.type == "video", Asset.analysis_state == "complete",
                                               Asset.classification_error.is_not(None))):
        if not visible(asset, user) or not asset.transcript or asset.category_source == "manual":
            continue
        asset.analysis_state = "preparing"
        asset.analysis_progress = None
        asset.classification_error = None
        queued += 1
    db.commit()
    return {"queued": queued}


@app.post("/api/upload", response_model=AssetRead, status_code=201)
async def upload_asset(db: SessionDep, user: UserDep, file: UploadFile = File(...)):
    if user.role == "agency":
        raise HTTPException(403, "Agency upload is not enabled in this demo")
    mime = file.content_type or ""
    if mime not in ALLOWED_MIME:
        raise HTTPException(400, "Choose a JPEG, PNG or MP4 file")
    limit = MAX_VIDEO_BYTES if mime == "video/mp4" else MAX_IMAGE_BYTES
    asset_id = f"NS-{uuid4().hex[:10].upper()}"
    stored_name = f"{uuid4().hex}{ALLOWED_MIME[mime]}"
    with TemporaryDirectory(prefix="atlas-upload-") as directory:
        path = Path(directory) / stored_name
        size = 0
        try:
            with path.open("wb") as output:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, f"Uploads of this type are limited to {limit // 1_000_000} MB")
                    output.write(chunk)
            if size == 0:
                raise HTTPException(400, "The file is empty")
            with path.open("rb") as source:
                signature = source.read(12)
            if not ((mime == "image/png" and signature.startswith(b"\x89PNG\r\n\x1a\n"))
                    or (mime == "image/jpeg" and signature.startswith(b"\xff\xd8\xff"))
                    or (mime == "video/mp4" and signature[4:8] == b"ftyp")):
                raise HTTPException(400, "File contents do not match the selected format")
            storage.save(stored_name, path, mime)
            if mime == "video/mp4":
                storage.thumbnail(stored_name)
            duration_seconds = probe_video_duration(path) if mime == "video/mp4" else None
            title = Path(file.filename or "Untitled").stem.replace("_", " ")[:160]
            asset = Asset(
                id=asset_id, title=title, type="video" if mime.startswith("video/") else "image",
                campaign="Unassigned", brand="Northstar", status="draft", rights=date(2027, 12, 31),
                audience="internal", tags=[], ai_tags=[],
                description="New upload awaiting metadata review.", owner_id=user.id, owner=user.name,
                size=f"{size / 1_000_000:.1f} MB", size_bytes=size,
                uploaded=date.today().strftime("%d %b %Y"),
                duration=duration_label(duration_seconds) if duration_seconds is not None else None,
                duration_seconds=duration_seconds,
                art="upload", file_name=stored_name, mime_type=mime, analysis_state="queued",
                transcript=[],
            )
            db.add(asset)
            mark_search_pending(db, asset)
            log_activity(db, f"{title} uploaded", user)
            db.commit()
            db.refresh(asset)
            return public_asset(asset, user)
        except Exception:
            storage.delete(stored_name)
            if mime == "video/mp4":
                storage.delete(str(Path(stored_name).with_suffix(".jpg")))
            raise
        finally:
            await file.close()


@app.get("/uploads/{asset_id}")
def get_uploaded_asset(asset_id: str, db: SessionDep, viewer: str | None = None,
                       expires: int | None = None, signature: str | None = None, download: bool = False,
                       range_header: Annotated[str | None, Header(alias="Range")] = None):
    viewer_id = verify_media_url(asset_id, "original", viewer, expires, signature)
    user = db.get(User, viewer_id)
    asset = db.get(Asset, asset_id)
    if not user or not user.active or not asset or not asset.file_name or not visible(asset, user):
        raise HTTPException(404, "File not found")
    filename = f"{asset.title}{Path(asset.file_name).suffix}" if download else None
    return storage.response(asset.file_name, asset.mime_type or "application/octet-stream", range_header, filename)


@app.get("/thumbnails/{asset_id}")
def get_video_thumbnail(asset_id: str, db: SessionDep, viewer: str | None = None,
                        expires: int | None = None, signature: str | None = None):
    viewer_id = verify_media_url(asset_id, "thumbnail", viewer, expires, signature)
    user = db.get(User, viewer_id)
    asset = db.get(Asset, asset_id)
    if not user or not user.active or not asset or asset.type != "video" or not asset.file_name or not visible(asset, user):
        raise HTTPException(404, "Thumbnail not found")
    thumbnail = storage.thumbnail(asset.file_name)
    if thumbnail is None:
        raise HTTPException(404, "Thumbnail not found")
    return storage.response(thumbnail, "image/jpeg")


@app.get("/demo-art/{theme}.svg")
def demo_art(theme: str):
    return Response(art_svg(theme), media_type="image/svg+xml")
