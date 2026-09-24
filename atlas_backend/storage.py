"""Local development and private Azure Blob asset storage."""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator, Protocol
from urllib.parse import quote

from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings
from fastapi import HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse

from .database import UPLOAD_DIR
from .thumbnails import ensure_video_thumbnail


class AssetStorage(Protocol):
    def save(self, name: str, source: Path, content_type: str) -> None: ...
    def delete(self, name: str) -> None: ...
    def exists(self, name: str) -> bool: ...
    def materialize(self, name: str) -> Iterator[Path]: ...
    def response(self, name: str, content_type: str, range_header: str | None = None,
                 download_name: str | None = None) -> Response: ...
    def thumbnail(self, video_name: str) -> str | None: ...


def valid_name(name: str) -> str:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError("Invalid asset storage name")
    return name


def thumbnail_name(video_name: str) -> str:
    return str(Path(valid_name(video_name)).with_suffix(".jpg"))


class LocalStorage:
    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def path(self, name: str) -> Path:
        return self.directory / valid_name(name)

    def save(self, name: str, source: Path, content_type: str) -> None:
        destination = self.path(name)
        if source != destination:
            import shutil
            shutil.copyfile(source, destination)

    def delete(self, name: str) -> None:
        self.path(name).unlink(missing_ok=True)

    def exists(self, name: str) -> bool:
        return self.path(name).is_file()

    @contextmanager
    def materialize(self, name: str) -> Iterator[Path]:
        path = self.path(name)
        if not path.is_file():
            raise FileNotFoundError(name)
        yield path

    def response(self, name: str, content_type: str, range_header: str | None = None,
                 download_name: str | None = None) -> Response:
        path = self.path(name)
        if not path.is_file():
            raise HTTPException(404, "File not found")
        return FileResponse(path, media_type=content_type, filename=download_name,
                            headers={"X-Content-Type-Options": "nosniff"})

    def thumbnail(self, video_name: str) -> str | None:
        result = ensure_video_thumbnail(self.path(video_name))
        return result.name if result else None


class BlobStorage:
    def __init__(self, container):
        self.container = container

    @classmethod
    def from_environment(cls) -> "BlobStorage":
        connection_string = os.getenv("ATLAS_BLOB_CONNECTION_STRING")
        if connection_string:
            service = BlobServiceClient.from_connection_string(connection_string, api_version="2023-11-03")
        else:
            account_url = os.environ["ATLAS_BLOB_ACCOUNT_URL"]
            service = BlobServiceClient(account_url=account_url, credential=DefaultAzureCredential())
        return cls(service.get_container_client(os.environ["ATLAS_BLOB_CONTAINER"]))

    def blob(self, name: str):
        return self.container.get_blob_client(valid_name(name))

    def save(self, name: str, source: Path, content_type: str) -> None:
        with source.open("rb") as stream:
            self.blob(name).upload_blob(stream, overwrite=True,
                                        content_settings=ContentSettings(content_type=content_type))

    def delete(self, name: str) -> None:
        try:
            self.blob(name).delete_blob()
        except ResourceNotFoundError:
            pass

    def exists(self, name: str) -> bool:
        return self.blob(name).exists()

    @contextmanager
    def materialize(self, name: str) -> Iterator[Path]:
        with TemporaryDirectory(prefix="atlas-asset-") as directory:
            path = Path(directory) / valid_name(name)
            try:
                with path.open("wb") as output:
                    download = self.blob(name).download_blob()
                    for chunk in download.chunks():
                        output.write(chunk)
            except ResourceNotFoundError as exc:
                raise FileNotFoundError(name) from exc
            yield path

    def response(self, name: str, content_type: str, range_header: str | None = None,
                 download_name: str | None = None) -> Response:
        blob = self.blob(name)
        try:
            size = blob.get_blob_properties().size
        except ResourceNotFoundError:
            raise HTTPException(404, "File not found") from None
        headers = {"Accept-Ranges": "bytes", "X-Content-Type-Options": "nosniff"}
        start, end, status = 0, size - 1, 200
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match or (not match[1] and not match[2]):
                raise HTTPException(416, "Invalid range", headers={"Content-Range": f"bytes */{size}"})
            if match[1]:
                start = int(match[1])
                end = min(int(match[2]), size - 1) if match[2] else size - 1
            else:
                start = max(0, size - int(match[2]))
            if size == 0 or start >= size or end < start:
                raise HTTPException(416, "Range not satisfiable", headers={"Content-Range": f"bytes */{size}"})
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        length = max(0, end - start + 1)
        headers["Content-Length"] = str(length)
        if download_name:
            headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(download_name)}"

        def chunks():
            try:
                if length:
                    yield from blob.download_blob(offset=start, length=length).chunks()
            except ResourceNotFoundError:
                return

        return StreamingResponse(chunks(), status_code=status, media_type=content_type, headers=headers)

    def thumbnail(self, video_name: str) -> str | None:
        name = thumbnail_name(video_name)
        if self.exists(name):
            return name
        try:
            with self.materialize(video_name) as path:
                result = ensure_video_thumbnail(path)
                if result is None:
                    return None
                self.save(name, result, "image/jpeg")
                return name
        except FileNotFoundError:
            return None


def create_storage() -> AssetStorage:
    backend = os.getenv("ATLAS_STORAGE_BACKEND", "local").lower()
    if backend == "local":
        return LocalStorage(UPLOAD_DIR)
    if backend == "blob":
        return BlobStorage.from_environment()
    raise ValueError("ATLAS_STORAGE_BACKEND must be local or blob")
