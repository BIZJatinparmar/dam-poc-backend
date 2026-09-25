"""Add pgvector-backed discovery index."""

from alembic import op
import sqlalchemy as sa

from atlas_backend.models import Vector1536


revision = "0003_semantic_discovery"
down_revision = "0002_speech_transcription"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "search_states",
        sa.Column("asset_id", sa.String(40), sa.ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("indexed_hash", sa.String(64)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error", sa.String(255)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "search_chunks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("asset_id", sa.String(40), sa.ForeignKey("assets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chunk_key", sa.String(80), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("start_seconds", sa.Float),
        sa.Column("end_seconds", sa.Float),
        sa.Column("embedding", Vector1536(), nullable=False),
        sa.UniqueConstraint("asset_id", "chunk_key"),
    )
    op.create_index("ix_search_chunks_asset_id", "search_chunks", ["asset_id"])
    op.execute("CREATE INDEX ix_search_chunks_embedding ON search_chunks USING hnsw (embedding vector_cosine_ops)")
    op.execute("CREATE INDEX ix_search_chunks_content ON search_chunks USING gin (to_tsvector('english', content))")


def downgrade() -> None:
    op.drop_table("search_chunks")
    op.drop_table("search_states")
