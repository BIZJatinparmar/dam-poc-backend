"""Short-lived asset URLs for browser media elements in the restricted demo."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from urllib.parse import urlencode

from fastapi import HTTPException

from .models import Asset, User


_KEY = (os.getenv("ATLAS_MEDIA_SIGNING_KEY") or "").encode() or secrets.token_bytes(32)
URL_LIFETIME_SECONDS = 3600


def _signature(asset_id: str, user_id: str, kind: str, expires: int) -> str:
    message = f"{asset_id}:{user_id}:{kind}:{expires}".encode()
    return hmac.new(_KEY, message, hashlib.sha256).hexdigest()


def signed_media_url(asset: Asset, user: User, kind: str) -> str:
    if not asset.file_name:
        raise ValueError("Asset has no uploaded media")
    expires = int(time.time()) + URL_LIFETIME_SECONDS
    query = urlencode({"viewer": user.id, "expires": expires,
                       "signature": _signature(asset.id, user.id, kind, expires)})
    path = "thumbnails" if kind == "thumbnail" else "uploads"
    return f"/{path}/{asset.id}?{query}"


def verify_media_url(asset_id: str, kind: str, viewer: str | None, expires: int | None,
                     signature: str | None) -> str:
    if not viewer or not expires or not signature or expires < int(time.time()):
        raise HTTPException(403, "Media link is missing or expired")
    expected = _signature(asset_id, viewer, kind, expires)
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(403, "Media link is invalid")
    return viewer
