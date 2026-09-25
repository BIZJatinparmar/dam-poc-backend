"""Store exact upload sizes and transcript-based video classifications."""

from alembic import op
import sqlalchemy as sa


revision = "0004_video_dashboard"
down_revision = "0003_semantic_discovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("assets", sa.Column("size_bytes", sa.BigInteger(), nullable=True))
    op.add_column("assets", sa.Column("video_category", sa.String(30), nullable=True))
    op.add_column("assets", sa.Column("video_format", sa.String(30), nullable=True))
    op.add_column("assets", sa.Column("classification_evidence", sa.Text(), nullable=True))
    op.add_column("assets", sa.Column("classification_error", sa.String(255), nullable=True))
    op.add_column("assets", sa.Column("category_source", sa.String(10), nullable=True))


def downgrade() -> None:
    op.drop_column("assets", "category_source")
    op.drop_column("assets", "classification_error")
    op.drop_column("assets", "classification_evidence")
    op.drop_column("assets", "video_format")
    op.drop_column("assets", "video_category")
    op.drop_column("assets", "size_bytes")
