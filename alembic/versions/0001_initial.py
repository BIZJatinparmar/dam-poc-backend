"""Initial PostgreSQL schema for the Atlas demo."""

from alembic import op
import sqlalchemy as sa


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("team", sa.String(100), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("initials", sa.String(4), nullable=False),
    )
    op.create_table(
        "assets",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("type", sa.String(10), nullable=False),
        sa.Column("campaign", sa.String(100), nullable=False),
        sa.Column("brand", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("rights", sa.Date(), nullable=False),
        sa.Column("audience", sa.String(20), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("ai_tags", sa.JSON(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("owner_id", sa.String(40), sa.ForeignKey("users.id")),
        sa.Column("owner", sa.String(120), nullable=False),
        sa.Column("size", sa.String(30), nullable=False),
        sa.Column("uploaded", sa.String(30), nullable=False),
        sa.Column("duration", sa.String(20)),
        sa.Column("art", sa.String(30), nullable=False),
        sa.Column("file_name", sa.String(255)),
        sa.Column("mime_type", sa.String(100)),
        sa.Column("analysis_state", sa.String(24)),
        sa.Column("analysis_progress", sa.Integer()),
        sa.Column("analysis_error", sa.String(255)),
        sa.Column("azure_video_id", sa.String(100)),
        sa.Column("azure_operation_url", sa.Text()),
        sa.Column("transcript", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "activity",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("text", sa.String(255), nullable=False),
        sa.Column("by", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("activity")
    op.drop_table("assets")
    op.drop_table("users")
