"""Azure analysis adapter and restartable, single-process demo worker."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlparse

import httpx
from sqlalchemy import Engine, case, select
from sqlalchemy.orm import Session

from .models import Asset
from .storage import AssetStorage, LocalStorage


ACTIVE_STATES = {"queued", "sending", "indexing", "preparing"}
TAG_LOCK = threading.RLock()
IMAGE_API_VERSION = "2025-11-01"


class AnalysisFailure(Exception):
    pass


def safe_http_error(exc: httpx.HTTPStatusError) -> str:
    """Give a useful Azure failure without exposing response identifiers or tokens."""
    response = exc.response
    if (response.status_code == 403 and exc.request.url.host == "management.azure.com"
            and exc.request.url.path.endswith("/generateAccessToken")):
        try:
            code = response.json().get("error", {}).get("code")
        except (ValueError, AttributeError, TypeError):
            code = None
        if code == "AuthorizationFailed":
            return ("Video Indexer access denied. Grant the app identity Video Indexer Account Contributor "
                    "on this account, then Retry.")
    return f"Azure analysis request failed ({response.status_code})"


def clean_tags(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        tag = re.sub(r"\s+", " ", value).strip()[:50]
        if tag and tag.casefold() not in seen:
            result.append(tag)
            seen.add(tag.casefold())
        if len(result) == 20:
            break
    return result


def video_insights(payload: dict) -> tuple[list[str], list[dict]]:
    videos = payload.get("videos") or []
    insights = (videos[0].get("insights", {}) if videos else payload.get("insights", {})) or {}
    tags = clean_tags([
        item.get("name", "") for group in ("labels", "keywords", "topics")
        for item in insights.get(group, []) if isinstance(item, dict)
    ])
    segments = []
    for item in insights.get("transcript", []):
        instances = item.get("instances") or []
        if not instances or not item.get("text"):
            continue
        span = instances[0]
        segments.append({
            "id": int(item["id"]), "start": span.get("start", "0:00:00"),
            "end": span.get("end", "0:00:00"), "text": item["text"].strip(),
        })
    return tags, segments


def image_tags(payload: dict) -> list[str]:
    contents = (payload.get("result") or {}).get("contents") or []
    fields = contents[0].get("fields", {}) if contents else {}
    tag_field = fields.get("tags") or fields.get("Tags") or {}
    return clean_tags([item.get("valueString", "") for item in tag_field.get("valueArray", [])])


def merge_tags(current: list[str], generated: list[str]) -> list[str]:
    return clean_tags([*current, *generated])


@contextmanager
def video_for_indexing(path: Path):
    """Convert codecs unsupported by Video Indexer, keeping the original upload intact."""
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
             "-of", "json", str(path)], capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError as exc:
        raise AnalysisFailure("FFmpeg is required to inspect uploaded videos") from exc
    except subprocess.TimeoutExpired as exc:
        raise AnalysisFailure("The uploaded video could not be inspected") from exc
    try:
        streams = json.loads(probe.stdout).get("streams", []) if probe.returncode == 0 else []
    except (ValueError, AttributeError):
        streams = []
    video = next((item.get("codec_name") for item in streams if item.get("codec_type") == "video"), None)
    audio = [item.get("codec_name") for item in streams if item.get("codec_type") == "audio"]
    if not video:
        raise AnalysisFailure("The uploaded MP4 has no readable video track")
    supported_video = {"h264", "hevc", "mpeg4", "mpeg2video", "mpeg1video", "vc1", "wmv3"}
    supported_audio = {"aac", "mp2", "mp3", "flac", "opus", "vorbis", "amr_nb", "amr_wb"}
    if video in supported_video and all(codec in supported_audio or str(codec).startswith("pcm_") for codec in audio):
        yield path
        return

    with TemporaryDirectory(prefix="atlas-vi-", dir=path.parent) as directory:
        converted = Path(directory) / "video.mp4"
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
                 "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                 "-movflags", "+faststart", str(converted)],
                capture_output=True, text=True, timeout=1800, stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise AnalysisFailure("FFmpeg is required to convert this video's codec") from exc
        except subprocess.TimeoutExpired as exc:
            raise AnalysisFailure("Video conversion timed out") from exc
        if result.returncode != 0 or not converted.is_file() or converted.stat().st_size == 0:
            raise AnalysisFailure("The video could not be converted to a supported format")
        yield converted


class AzureAnalysisClient:
    def __init__(self):
        self.cu_endpoint = os.getenv("ATLAS_CU_ENDPOINT", "").rstrip("/")
        self.cu_key = os.getenv("ATLAS_CU_KEY", "")
        self.cu_analyzer = os.getenv("ATLAS_CU_ANALYZER_ID", "atlas_image_tags")
        self.vi_subscription = os.getenv("ATLAS_VI_SUBSCRIPTION_ID", "")
        self.vi_group = os.getenv("ATLAS_VI_RESOURCE_GROUP", "")
        self.vi_account_name = os.getenv("ATLAS_VI_ACCOUNT_NAME", "")
        self.vi_account_id = os.getenv("ATLAS_VI_ACCOUNT_ID", "")
        self.vi_location = os.getenv("ATLAS_VI_LOCATION", "")

    def require_config(self, kind: str) -> None:
        if kind == "image" and not (self.cu_endpoint and self.cu_key and self.cu_analyzer):
            raise AnalysisFailure("Azure Content Understanding is not configured")
        if kind == "video" and not all((self.vi_subscription, self.vi_group, self.vi_account_name, self.vi_account_id, self.vi_location)):
            raise AnalysisFailure("Azure Video Indexer is not configured")

    def _vi_token(self) -> str:
        identity_endpoint = os.getenv("IDENTITY_ENDPOINT")
        identity_header = os.getenv("IDENTITY_HEADER")
        tenant = os.getenv("ATLAS_AZURE_TENANT_ID")
        client_id = os.getenv("ATLAS_AZURE_CLIENT_ID")
        client_secret = os.getenv("ATLAS_AZURE_CLIENT_SECRET")
        with httpx.Client(timeout=30, trust_env=True) as client:
            if identity_endpoint and identity_header:
                params = {"resource": "https://management.azure.com/", "api-version": "2019-08-01"}
                if client_id:
                    params["client_id"] = client_id
                token_response = client.get(identity_endpoint, params=params,
                                            headers={"X-IDENTITY-HEADER": identity_header})
            elif tenant and client_id and client_secret:
                token_response = client.post(
                    f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
                    data={"grant_type": "client_credentials", "client_id": client_id,
                          "client_secret": client_secret, "scope": "https://management.azure.com/.default"},
                )
            else:
                raise AnalysisFailure("Azure Video Indexer identity is not configured")
            token_response.raise_for_status()
            arm_token = token_response.json()["access_token"]
        url = (f"https://management.azure.com/subscriptions/{self.vi_subscription}/resourceGroups/"
               f"{self.vi_group}/providers/Microsoft.VideoIndexer/accounts/{self.vi_account_name}/"
               "generateAccessToken?api-version=2025-04-01")
        with httpx.Client(timeout=30, trust_env=True) as client:
            response = client.post(url, headers={"Authorization": f"Bearer {arm_token}"},
                                   json={"permissionType": "Contributor", "scope": "Account"})
            response.raise_for_status()
            return response.json()["accessToken"]

    def _vi_url(self, suffix: str) -> str:
        return f"https://api.videoindexer.ai/{self.vi_location}/Accounts/{self.vi_account_id}/{suffix}"

    def submit_video(self, asset: Asset, path: Path) -> str:
        with video_for_indexing(path) as prepared:
            token = self._vi_token()
            with httpx.Client(timeout=httpx.Timeout(300, connect=30), trust_env=True) as client, prepared.open("rb") as source:
                response = client.post(
                    self._vi_url("Videos"), headers={"Authorization": f"Bearer {token}"},
                    params={"name": asset.title[:80], "privacy": "Private", "streamingPreset": "NoStreaming",
                            "externalId": asset.id},
                    files={"fileName": (prepared.name, source, "video/mp4")},
                )
                response.raise_for_status()
                return response.json()["id"]

    def poll_video(self, video_id: str) -> tuple[str, int | None, dict | None]:
        token = self._vi_token()
        with httpx.Client(timeout=30, trust_env=True) as client:
            headers = {"Authorization": f"Bearer {token}"}
            response = client.get(self._vi_url(f"Videos/{video_id}/Index"), headers=headers,
                                  params={"includeSummarizedInsights": "false"})
            if response.status_code == 409:
                # Video Indexer can briefly report a conflict while a new job starts.
                return "indexing", None, None
            response.raise_for_status()
            status = response.json()
            state = str(status.get("state", "")).lower()
            if state in {"failed", "error", "quarantined"}:
                videos = status.get("videos") or []
                if videos and videos[0].get("failureCode") == "InvalidFileFormat":
                    raise AnalysisFailure("Azure Video Indexer rejected an unsupported video format. Convert the video to H.264/AAC and upload it again.")
                raise AnalysisFailure("Azure Video Indexer could not index this video")
            if state in {"processed", "2"}:
                return "complete", 100, status
            if state not in {"uploaded", "uploading", "processing", "queued", "1"}:
                raise AnalysisFailure("Azure Video Indexer returned an unknown processing state")
        reported_progress = status.get("processingProgress", "")
        match = re.search(r"\d+", str(reported_progress))
        progress = min(int(match.group()), 99) if match else None
        return "indexing", progress, None

    def submit_image(self, path: Path, mime_type: str) -> str:
        url = f"{self.cu_endpoint}/contentunderstanding/analyzers/{self.cu_analyzer}:analyzeBinary"
        with httpx.Client(timeout=60, trust_env=True) as client, path.open("rb") as source:
            response = client.post(url, params={"api-version": IMAGE_API_VERSION},
                                   headers={"Ocp-Apim-Subscription-Key": self.cu_key, "Content-Type": mime_type},
                                   content=source.read())
            response.raise_for_status()
        operation_url = response.headers.get("Operation-Location", "")
        if not operation_url or urlparse(operation_url).netloc != urlparse(self.cu_endpoint).netloc:
            raise AnalysisFailure("Content Understanding did not return a valid operation URL")
        return operation_url

    def poll_image(self, operation_url: str) -> tuple[str, dict | None]:
        if urlparse(operation_url).netloc != urlparse(self.cu_endpoint).netloc:
            raise AnalysisFailure("Invalid Content Understanding operation URL")
        with httpx.Client(timeout=30, trust_env=True) as client:
            response = client.get(operation_url, headers={"Ocp-Apim-Subscription-Key": self.cu_key})
            response.raise_for_status()
            payload = response.json()
        status = str(payload.get("status", "")).lower()
        if status == "succeeded":
            return "complete", payload
        if status in {"failed", "canceled"}:
            raise AnalysisFailure("Azure Content Understanding could not analyze this image")
        if status not in {"running", "notstarted"}:
            raise AnalysisFailure("Azure Content Understanding returned an unknown processing state")
        return "indexing", None


def run_analysis_step(engine: Engine, asset_storage: AssetStorage | Path, client: AzureAnalysisClient | None = None,
                      asset_id: str | None = None) -> bool:
    """Advance one asset by one remote operation; never hold a DB transaction during HTTP."""
    client = client or AzureAnalysisClient()
    storage = LocalStorage(asset_storage) if isinstance(asset_storage, Path) else asset_storage
    with Session(engine, expire_on_commit=False) as db:
        asset = (db.get(Asset, asset_id) if asset_id else
                 db.scalars(select(Asset).where(Asset.analysis_state.in_(ACTIVE_STATES))
                            .order_by(case((Asset.analysis_state == "queued", 0), else_=1),
                                      Asset.created_at, Asset.id)).first())
        if asset is None or asset.analysis_state not in ACTIVE_STATES:
            return False
        asset_id = asset.id
        kind = asset.type
        state = asset.analysis_state
        video_id = asset.azure_video_id
        operation_url = asset.azure_operation_url
        file_name = asset.file_name
        if state == "queued":
            asset.analysis_state = "sending"
            db.commit()

    try:
        client.require_config(kind)
        if not file_name or not storage.exists(file_name):
            raise AnalysisFailure("The uploaded file is missing")
        if state in {"queued", "sending"}:
            with storage.materialize(file_name) as path:
                if kind == "video":
                    video_id = video_id or client.submit_video(asset, path)
                else:
                    operation_url = operation_url or client.submit_image(path, asset.mime_type or "image/jpeg")
            with Session(engine) as db:
                current = db.get(Asset, asset_id)
                current.azure_video_id = video_id
                current.azure_operation_url = operation_url
                current.analysis_state = "indexing"
                db.commit()
            return True
        if state == "preparing":
            # A restart here means the remote result must be fetched again.
            state = "indexing"
        if kind == "video":
            if not video_id:
                raise AnalysisFailure("Video Indexer job ID is missing")
            result, progress, payload = client.poll_video(video_id)
            tags, transcript = video_insights(payload) if payload else ([], [])
        else:
            if not operation_url:
                raise AnalysisFailure("Content Understanding operation ID is missing")
            result, payload = client.poll_image(operation_url)
            progress = None
            tags, transcript = (image_tags(payload), []) if payload else ([], [])
        with Session(engine) as db:
            current = db.get(Asset, asset_id)
            if result == "complete":
                current.analysis_state = "preparing"
                db.commit()
                with TAG_LOCK:
                    db.refresh(current)
                    current.tags = merge_tags(current.tags, tags)
                    current.ai_tags = tags
                    current.transcript = transcript
                    current.analysis_state = "complete"
                    current.analysis_progress = 100
                    current.analysis_error = None
                    db.commit()
            else:
                current.analysis_state = "indexing"
                current.analysis_progress = progress
                db.commit()
        return True
    except Exception as exc:
        if isinstance(exc, AnalysisFailure):
            message = str(exc)
        elif isinstance(exc, httpx.HTTPStatusError):
            message = safe_http_error(exc)
        else:
            message = "Azure analysis is temporarily unavailable"
        with Session(engine) as db:
            current = db.get(Asset, asset_id)
            current.analysis_state = "failed"
            current.analysis_error = message
            current.analysis_progress = None
            db.commit()
        return True


async def analysis_worker(engine: Engine, asset_storage: AssetStorage | Path) -> None:
    while True:
        try:
            with Session(engine) as db:
                ids = list(db.scalars(select(Asset.id).where(Asset.analysis_state.in_(ACTIVE_STATES))
                                      .order_by(Asset.created_at, Asset.id)))
            for asset_id in ids:
                await asyncio.to_thread(run_analysis_step, engine, asset_storage, None, asset_id)
        except Exception:
            # Keep the demo worker alive; the asset-level function records job failures.
            pass
        await asyncio.sleep(5)
