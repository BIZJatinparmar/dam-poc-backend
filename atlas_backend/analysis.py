"""Azure analysis adapter and restartable, single-process demo worker."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy import Engine, case, select
from sqlalchemy.orm import Session

from .models import Asset
from .storage import AssetStorage, BlobStorage, LocalStorage


ACTIVE_STATES = {"queued", "sending", "indexing", "preparing"}
TAG_LOCK = threading.RLock()
IMAGE_API_VERSION = "2025-11-01"
SPEECH_API_VERSION = "2025-10-15"
FOUNDRY_CHUNK_CHARS = 12_000
LOGGER = logging.getLogger(__name__)


class AnalysisFailure(Exception):
    pass


class SpeechJobFailure(AnalysisFailure):
    """A completed remote job that cannot yield a transcript."""


def speech_error_nodes(response: httpx.Response) -> list[dict]:
    try:
        payload = response.json()
    except (ValueError, TypeError):
        return []
    error = payload.get("error", payload) if isinstance(payload, dict) else None
    nodes = []
    for _ in range(3):
        if not isinstance(error, dict):
            break
        nodes.append(error)
        error = error.get("innerError")
    return nodes


def safe_speech_message(message: str) -> str:
    """Retain Azure's explanation while removing URLs and credential-like values."""
    message = re.sub(r"https?://[^\s<>\"']+", "<URL>", message, flags=re.I)
    message = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "<EMAIL>", message)
    message = re.sub(r"(?i)\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", "<HOST>", message)
    message = re.sub(r"(['\"])([^'\"]+)\1",
                     lambda match: match.group(0) if match.group(2) in {"Standard", "Free", "S0", "F0"}
                     else "<VALUE>", message)
    message = re.sub(r"(?i)(\b(?:key|token|secret|signature|password|authorization|sig)\b\s*[:=]\s*)\S+",
                     r"\1<REDACTED>", message)
    message = re.sub(r"(?<![A-Za-z0-9])[A-Za-z0-9_+/-]{20,}={0,2}(?![A-Za-z0-9])",
                     "<VALUE>", message)
    return re.sub(r"\s+", " ", message).strip()[:240]


def safe_http_error(exc: httpx.HTTPStatusError) -> str:
    """Give a useful Azure failure without exposing response identifiers or tokens."""
    response = exc.response
    if exc.request.url.path == "/speechtotext/transcriptions:submit":
        codes = [node["code"] for node in speech_error_nodes(response)
                 if isinstance(node.get("code"), str)
                 and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", node["code"])]
        if codes:
            return f"Azure Speech submission failed ({response.status_code}, {': '.join(codes)})"
        return f"Azure Speech submission failed ({response.status_code})"
    return f"Azure analysis request failed ({response.status_code})"


def log_speech_submit_failure(exc: httpx.HTTPStatusError, endpoint: str) -> None:
    nodes = speech_error_nodes(exc.response)
    messages = [safe_speech_message(node["message"]) for node in nodes
                if isinstance(node.get("message"), str)]
    request_id = exc.response.headers.get("apim-request-id") or exc.response.headers.get("x-ms-request-id", "")
    if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", request_id):
        request_id = "unavailable"
    LOGGER.warning("%s; endpoint=%s; request_id=%s; detail=%s",
                   safe_http_error(exc), urlparse(endpoint).hostname, request_id,
                   " | ".join(messages)[:480] or "unavailable")


def clean_tags(values: list[str], limit: int = 20) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        tag = re.sub(r"\s+", " ", value).strip()[:50]
        if tag and tag.casefold() not in seen:
            result.append(tag)
            seen.add(tag.casefold())
        if len(result) == limit:
            break
    return result


def grounded_tag(item: dict, source: str) -> bool:
    name, evidence = item.get("name"), item.get("evidence")
    if not isinstance(name, str) or not isinstance(evidence, str):
        return False
    evidence = evidence.strip().casefold()
    tag_words = set(re.findall(r"\w+", name.casefold()))
    evidence_words = set(re.findall(r"\w+", evidence))
    return (len(evidence) >= 4 and evidence in source.casefold() and bool(tag_words)
            and tag_words <= evidence_words)


def timestamp(ticks: int) -> str:
    milliseconds = max(0, ticks // 10_000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours}:{minutes:02}:{seconds:02}.{milliseconds:03}"


def speech_segments(payload: dict) -> list[dict]:
    segments = []
    for phrase in payload.get("recognizedPhrases", []):
        if phrase.get("recognitionStatus") != "Success":
            continue
        best = (phrase.get("nBest") or [{}])[0]
        content = best.get("display", "").strip()
        if not content:
            continue
        start = int(phrase.get("offsetInTicks", 0))
        end = start + int(phrase.get("durationInTicks", 0))
        segments.append({"start_ticks": start, "end_ticks": end, "text": content})
    segments.sort(key=lambda item: (item["start_ticks"], item["end_ticks"]))
    return [{"id": index, "start": timestamp(item["start_ticks"]),
             "end": timestamp(item["end_ticks"]), "text": item["text"]}
            for index, item in enumerate(segments, 1)]


def image_tags(payload: dict) -> list[str]:
    contents = (payload.get("result") or {}).get("contents") or []
    fields = contents[0].get("fields", {}) if contents else {}
    tag_field = fields.get("tags") or fields.get("Tags") or {}
    return clean_tags([item.get("valueString", "") for item in tag_field.get("valueArray", [])])


def merge_tags(current: list[str], generated: list[str]) -> list[str]:
    return clean_tags([*current, *generated])


def extract_audio(path: Path, output: Path) -> bool:
    """Return false for a silent video; keep leading timeline silence in extracted audio."""
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
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
    if not any(item.get("codec_type") == "video" for item in streams):
        raise AnalysisFailure("The uploaded MP4 has no readable video track")
    if not any(item.get("codec_type") == "audio" for item in streams):
        return False
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
             "-map", "0:a:0", "-vn", "-sn", "-dn", "-ac", "1", "-ar", "16000",
             "-af", "aresample=async=1:first_pts=0", "-c:a", "flac", str(output)],
            capture_output=True, text=True, timeout=1800, stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise AnalysisFailure("FFmpeg is required to extract audio") from exc
    except subprocess.TimeoutExpired as exc:
        raise AnalysisFailure("Audio extraction timed out") from exc
    if result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        raise AnalysisFailure("The video audio could not be extracted")
    return True


class AzureAnalysisClient:
    def __init__(self):
        self.cu_endpoint = os.getenv("ATLAS_CU_ENDPOINT", "").rstrip("/")
        self.cu_key = os.getenv("ATLAS_CU_KEY", "")
        self.cu_analyzer = os.getenv("ATLAS_CU_ANALYZER_ID", "atlas_image_tags")
        self.speech_endpoint = os.getenv("ATLAS_SPEECH_ENDPOINT", "").rstrip("/")
        self.speech_key = os.getenv("ATLAS_SPEECH_KEY", "")
        self.speech_locale = os.getenv("ATLAS_SPEECH_LOCALE", "en-US")
        self.audio_container = os.getenv("ATLAS_SPEECH_AUDIO_CONTAINER", "")
        self.foundry_endpoint = os.getenv("ATLAS_FOUNDRY_ENDPOINT", "").rstrip("/")
        self.foundry_key = os.getenv("ATLAS_FOUNDRY_KEY", "")
        self.foundry_deployment = os.getenv("ATLAS_FOUNDRY_DEPLOYMENT", "gpt-5.4-nano")

    def require_config(self, kind: str) -> None:
        if kind == "image" and not (self.cu_endpoint and self.cu_key and self.cu_analyzer):
            raise AnalysisFailure("Azure Content Understanding is not configured")
        if kind == "video" and not all((self.speech_endpoint, self.speech_key, self.audio_container)):
            raise AnalysisFailure("Azure Speech batch transcription is not configured")
        if kind == "video" and not (os.getenv("ATLAS_BLOB_ACCOUNT_URL") or os.getenv("ATLAS_BLOB_CONNECTION_STRING")):
            raise AnalysisFailure("Temporary audio Blob storage is not configured")

    def audio_storage(self) -> BlobStorage:
        return BlobStorage.from_environment(self.audio_container)

    def stage_audio(self, asset: Asset, path: Path) -> str | None:
        name = f"{asset.id}.flac"
        with TemporaryDirectory(prefix="atlas-audio-") as directory:
            audio = Path(directory) / name
            if not extract_audio(path, audio):
                return None
            self.audio_storage().save(name, audio, "audio/flac")
        return name

    def delete_audio(self, name: str | None) -> None:
        if name:
            self.audio_storage().delete(name)

    def _speech_url(self, suffix: str) -> str:
        return f"{self.speech_endpoint}/speechtotext/{suffix}"

    def _speech_headers(self) -> dict[str, str]:
        return {"Ocp-Apim-Subscription-Key": self.speech_key}

    def _speech_job_url(self, url: str) -> str:
        parsed = urlparse(url)
        match = re.fullmatch(r"/speechtotext/transcriptions/([A-Za-z0-9-]+)", parsed.path)
        host = parsed.hostname or ""
        configured_host = urlparse(self.speech_endpoint).hostname
        if (parsed.scheme != "https" or not match or
                (host != configured_host and
                 not host.endswith((".api.cognitive.microsoft.com", ".cognitiveservices.azure.com")))):
            raise AnalysisFailure("Azure Speech returned an invalid job URL")
        return f"{self._speech_url(f'transcriptions/{match[1]}')}?api-version={SPEECH_API_VERSION}"

    def submit_video(self, asset: Asset, audio_blob: str) -> str:
        blob = self.audio_storage().blob(audio_blob)
        body = {"displayName": asset.id, "locale": self.speech_locale,
                "contentUrls": [blob.url],
                "properties": {"wordLevelTimestampsEnabled": False, "timeToLiveHours": 48}}
        with httpx.Client(timeout=30, trust_env=True) as client:
            response = client.post(self._speech_url("transcriptions:submit"),
                                   params={"api-version": SPEECH_API_VERSION},
                                   headers=self._speech_headers(), json=body)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                log_speech_submit_failure(exc, self.speech_endpoint)
                raise
        location = response.headers.get("Location")
        try:
            payload = response.json()
        except ValueError:
            payload = None
        self_url = payload.get("self") if isinstance(payload, dict) else None
        candidates = (location, self_url)
        for source, candidate in zip(("Location", "self"), candidates):
            if not isinstance(candidate, str) or not candidate:
                continue
            resolved = urljoin(str(response.request.url), candidate)
            try:
                return self._speech_job_url(resolved)
            except AnalysisFailure:
                parsed = urlparse(resolved)
                endpoint_host = urlparse(self.speech_endpoint).hostname
                host_allowed = parsed.hostname == endpoint_host or bool(
                    parsed.hostname and parsed.hostname.endswith(
                        (".api.cognitive.microsoft.com", ".cognitiveservices.azure.com")))
                path_valid = bool(re.fullmatch(r"/speechtotext/transcriptions/[A-Za-z0-9-]+",
                                               parsed.path))
                LOGGER.warning("Azure Speech rejected %s job link; https=%s; host_allowed=%s; path_valid=%s",
                               source, parsed.scheme == "https", host_allowed, path_valid)
                continue
        LOGGER.warning("Azure Speech submitted a job without a usable URL; location_present=%s; self_present=%s",
                       bool(candidates[0]), bool(candidates[1]))
        raise AnalysisFailure("Azure Speech returned an invalid job URL")

    def poll_video(self, job_url: str) -> tuple[str, dict | None]:
        job_url = self._speech_job_url(job_url)
        with httpx.Client(timeout=30, trust_env=True) as client:
            response = client.get(job_url, headers=self._speech_headers())
            response.raise_for_status()
            job = response.json()
            status = str(job.get("status", "")).lower()
            if status in {"notstarted", "running"}:
                return "indexing", None
            if status != "succeeded":
                if status == "failed":
                    raise SpeechJobFailure("Azure Speech batch transcription failed")
                raise AnalysisFailure("Azure Speech returned an unknown job state")
            job_id = urlparse(job_url).path.rsplit("/", 1)[-1]
            files_url = f"{self._speech_url(f'transcriptions/{job_id}/files')}?api-version={SPEECH_API_VERSION}"
            files = client.get(files_url, headers=self._speech_headers())
            files.raise_for_status()
            transcript_files = [item for item in files.json().get("values", []) if item.get("kind") == "Transcription"]
            if len(transcript_files) != 1:
                raise AnalysisFailure("Azure Speech did not return one transcript file")
            content_url = transcript_files[0].get("links", {}).get("contentUrl", "")
            parsed = urlparse(content_url)
            if parsed.scheme != "https" or not parsed.netloc.endswith(".blob.core.windows.net"):
                raise AnalysisFailure("Azure Speech returned an invalid transcript URL")
            result = client.get(content_url)
            result.raise_for_status()
            return "complete", result.json()

    def _foundry_tags(self, source: str, *, candidates: bool = False) -> list[str]:
        schema = {"type": "object", "properties": {"tags": {"type": "array", "items": {
            "type": "object", "properties": {"name": {"type": "string"}, "evidence": {"type": "string"}},
            "required": ["name", "evidence"], "additionalProperties": False}}},
            "required": ["tags"], "additionalProperties": False}
        instruction = ("Choose up to 20 concise subject tags from these candidates. Preserve each tag's evidence exactly. "
                       if candidates else "Extract up to 10 concise subject tags grounded in the transcript. ")
        instruction += ("Every tag needs a short, exact supporting quote from the supplied text. "
                        "Use words found in that quote for each tag. "
                        "Never infer visual content, people, brands, or places not stated in the text.")
        body = {"model": self.foundry_deployment, "input": [
            {"role": "system", "content": instruction}, {"role": "user", "content": source}],
            "reasoning": {"effort": "none"},
            "max_output_tokens": 1200,
            "text": {"format": {"type": "json_schema", "name": "transcript_tags",
                                "schema": schema, "strict": True}}}
        endpoint = self.foundry_endpoint.rstrip("/")
        responses_url = (f"{endpoint}/responses" if endpoint.endswith("/openai/v1")
                         else f"{endpoint}/openai/v1/responses")
        with httpx.Client(timeout=90, trust_env=True) as client:
            response = client.post(responses_url,
                                   headers={"api-key": self.foundry_key}, json=body)
            response.raise_for_status()
            result = response.json()
        output = result.get("output_text") or "".join(
            part.get("text", "") for item in result.get("output", [])
            for part in item.get("content", []) if part.get("type") == "output_text")
        if not output:
            raise AnalysisFailure("Foundry returned no tags")
        tags = json.loads(output).get("tags", [])
        return clean_tags([item["name"] for item in tags if isinstance(item, dict)
                           and grounded_tag(item, source)], limit=200)

    def tag_transcript(self, transcript: list[dict]) -> list[str]:
        text = " ".join(segment["text"] for segment in transcript if segment.get("text"))
        if not text:
            return []
        if not self.foundry_endpoint or not self.foundry_key:
            raise AnalysisFailure("Foundry transcript tagging is not configured")
        chunks = []
        words = text.split()
        current = []
        size = 0
        for word in words:
            if current and size + len(word) + 1 > FOUNDRY_CHUNK_CHARS:
                chunks.append(" ".join(current))
                current, size = [], 0
            current.append(word)
            size += len(word) + 1
        if current:
            chunks.append(" ".join(current))
        candidates = clean_tags([tag for chunk in chunks for tag in self._foundry_tags(chunk)], limit=200)
        if len(candidates) <= 20:
            return candidates
        # The second pass only selects from already grounded candidates.
        source = "\n".join(f"{tag} | {tag}" for tag in candidates)
        allowed = {tag.casefold(): tag for tag in candidates}
        return clean_tags([allowed[tag.casefold()] for tag in self._foundry_tags(source, candidates=True)
                           if tag.casefold() in allowed])

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
        speech_job_url = asset.speech_job_url
        audio_blob = asset.speech_audio_blob
        operation_url = asset.azure_operation_url
        file_name = asset.file_name
        if state == "queued":
            asset.analysis_state = "sending"
            db.commit()

    try:
        if state != "preparing":
            client.require_config(kind)
        if state in {"queued", "sending"}:
            if not file_name or not storage.exists(file_name):
                raise AnalysisFailure("The uploaded file is missing")
            if kind == "video":
                if not audio_blob or not client.audio_storage().exists(audio_blob):
                    with storage.materialize(file_name) as path:
                        audio_blob = client.stage_audio(asset, path)
                    if audio_blob is None:
                        with Session(engine) as db:
                            current = db.get(Asset, asset_id)
                            current.transcript = []
                            current.ai_tags = []
                            current.tag_error = None
                            current.analysis_state = "complete"
                            current.analysis_progress = 100
                            db.commit()
                        return True
                    with Session(engine) as db:
                        current = db.get(Asset, asset_id)
                        current.speech_audio_blob = audio_blob
                        db.commit()
                speech_job_url = speech_job_url or client.submit_video(asset, audio_blob)
            else:
                with storage.materialize(file_name) as path:
                    operation_url = operation_url or client.submit_image(path, asset.mime_type or "image/jpeg")
            with Session(engine) as db:
                current = db.get(Asset, asset_id)
                current.speech_job_url = speech_job_url
                current.azure_operation_url = operation_url
                current.analysis_state = "indexing"
                db.commit()
            return True
        if state == "preparing":
            if kind == "video":
                try:
                    tags = client.tag_transcript(asset.transcript)
                    tag_error = None
                except Exception as exc:
                    if isinstance(exc, httpx.HTTPStatusError):
                        codes = [node["code"] for node in speech_error_nodes(exc.response)
                                 if isinstance(node.get("code"), str)
                                 and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", node["code"])]
                        LOGGER.warning("Azure Foundry transcript tagging failed; status=%s; code=%s",
                                       exc.response.status_code, codes[0] if codes else "unavailable")
                    else:
                        LOGGER.warning("Azure Foundry transcript tagging failed; error_type=%s",
                                       type(exc).__name__)
                    tags = []
                    tag_error = "Transcript ready, but AI tags could not be generated. Retry tags."
                with Session(engine) as db, TAG_LOCK:
                    current = db.get(Asset, asset_id)
                    current.tags = merge_tags(current.tags, tags)
                    current.ai_tags = tags
                    current.tag_error = tag_error
                    current.analysis_state = "complete"
                    current.analysis_progress = 100
                    current.analysis_error = None
                    db.commit()
                return True
            # Image results can be fetched again after a restart.
            state = "indexing"
        if kind == "video":
            if not speech_job_url:
                raise AnalysisFailure("Azure Speech job URL is missing")
            result, payload = client.poll_video(speech_job_url)
            if result == "complete":
                transcript = speech_segments(payload)
                with Session(engine) as db:
                    current = db.get(Asset, asset_id)
                    current.transcript = transcript
                    current.analysis_state = "preparing"
                    current.analysis_progress = None
                    current.speech_job_url = None
                    current.speech_audio_blob = None
                    db.commit()
                try:
                    client.delete_audio(audio_blob)
                except Exception:
                    pass  # Blob lifecycle cleanup is the backup.
                return True
            progress = None
            tags, transcript = [], []
        else:
            if not operation_url:
                raise AnalysisFailure("Content Understanding operation ID is missing")
            result, payload = client.poll_image(operation_url)
            progress = None
            tags, transcript = (image_tags(payload), []) if payload else ([], [])
        with Session(engine) as db:
            current = db.get(Asset, asset_id)
            if result == "complete":
                with TAG_LOCK:
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
            if kind == "video" and (isinstance(exc, SpeechJobFailure) or
                                    (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404)):
                current.speech_job_url = None
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
