from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


Role = Literal["admin", "editor", "agency"]
AssetStatus = Literal["draft", "in_review", "approved", "published"]
AssetType = Literal["image", "video"]
Audience = Literal["internal", "external"]


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    email: str
    role: Role
    team: str
    active: bool
    initials: str


class UserCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    role: Role = "agency"


class UserUpdate(BaseModel):
    active: bool


class AssetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    title: str
    type: AssetType
    campaign: str
    brand: str
    status: AssetStatus
    rights: date
    audience: Audience
    tags: list[str]
    ai_tags: list[str]
    description: str
    owner: str
    size: str
    uploaded: str
    art: str
    duration: str | None = None
    url: str | None = None
    thumbnail_url: str | None = None
    analysis_state: str | None = None
    analysis_progress: int | None = None
    analysis_error: str | None = None
    tag_error: str | None = None


class TranscriptSegment(BaseModel):
    id: int
    start: str
    end: str
    text: str


class AssetDetail(AssetRead):
    transcript: list[TranscriptSegment]


class TranscriptUpdate(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class AssetUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=4000)
    campaign: str | None = Field(default=None, min_length=1, max_length=100)
    rights: date | None = None
    audience: Audience | None = None
    tags: list[str] | None = Field(default=None, max_length=20)
    tag_additions: list[str] | None = Field(default=None, max_length=20)
    tag_removals: list[str] | None = Field(default=None, max_length=20)
    status: AssetStatus | None = None


class Summary(BaseModel):
    total: int
    images: int
    videos: int
    in_review: int
    published: int


class ActivityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    text: str
    by: str
    time: str
