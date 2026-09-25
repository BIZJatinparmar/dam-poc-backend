"""Store measured video duration for dashboard reporting."""

from alembic import op
import sqlalchemy as sa


revision = "0005_video_duration"
down_revision = "0004_video_dashboard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("assets", sa.Column("duration_seconds", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("assets", "duration_seconds")
