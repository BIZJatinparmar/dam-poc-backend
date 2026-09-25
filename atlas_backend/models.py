from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Float, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.types import UserDefinedType
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Vector1536(UserDefinedType):
    cache_ok = True

    def get_col_spec(self, **_kw) -> str:
        return "vector(1536)"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    team: Mapped[str] = mapped_column(String(100), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    initials: Mapped[str] = mapped_column(String(4), nullable=False)


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    type: Mapped[str] = mapped_column(String(10), nullable=False)
    campaign: Mapped[str] = mapped_column(String(100), nullable=False)
    brand: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    rights: Mapped[date] = mapped_column(Date, nullable=False)
    audience: Mapped[str] = mapped_column(String(20), nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    ai_tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    owner: Mapped[str] = mapped_column(String(120), nullable=False)
    size: Mapped[str] = mapped_column(String(30), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    uploaded: Mapped[str] = mapped_column(String(30), nullable=False)
    video_category: Mapped[str | None] = mapped_column(String(30))
    video_format: Mapped[str | None] = mapped_column(String(30))
    classification_evidence: Mapped[str | None] = mapped_column(Text)
    classification_error: Mapped[str | None] = mapped_column(String(255))
    category_source: Mapped[str | None] = mapped_column(String(10))
    duration: Mapped[str | None] = mapped_column(String(20))
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    art: Mapped[str] = mapped_column(String(30), nullable=False)
    file_name: Mapped[str | None] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(100))
    analysis_state: Mapped[str | None] = mapped_column(String(24))
    analysis_progress: Mapped[int | None] = mapped_column(Integer)
    analysis_error: Mapped[str | None] = mapped_column(String(255))
    speech_job_url: Mapped[str | None] = mapped_column(Text)
    speech_audio_blob: Mapped[str | None] = mapped_column(String(255))
    tag_error: Mapped[str | None] = mapped_column(String(255))
    azure_operation_url: Mapped[str | None] = mapped_column(Text)
    transcript: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    @property
    def url(self) -> str | None:
        return f"/uploads/{self.id}" if self.file_name else None

    @property
    def thumbnail_url(self) -> str | None:
        return f"/thumbnails/{self.id}" if self.type == "video" and self.file_name else None


class Activity(Base):
    __tablename__ = "activity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    text: Mapped[str] = mapped_column(String(255), nullable=False)
    by: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    @property
    def time(self) -> str:
        return self.created_at.strftime("%d %b %Y, %H:%M")


class SearchState(Base):
    __tablename__ = "search_states"

    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    indexed_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    error: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class SearchChunk(Base):
    __tablename__ = "search_chunks"
    __table_args__ = (UniqueConstraint("asset_id", "chunk_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True)
    chunk_key: Mapped[str] = mapped_column(String(80), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    start_seconds: Mapped[float | None] = mapped_column(Float)
    end_seconds: Mapped[float | None] = mapped_column(Float)
    embedding: Mapped[str] = mapped_column(Vector1536(), nullable=False)
