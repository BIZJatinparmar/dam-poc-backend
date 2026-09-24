"""Track Speech batch jobs and Foundry tag failures."""

from alembic import op
import sqlalchemy as sa


revision = "0002_speech_transcription"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("assets", sa.Column("speech_job_url", sa.Text()))
    op.add_column("assets", sa.Column("speech_audio_blob", sa.String(255)))
    op.add_column("assets", sa.Column("tag_error", sa.String(255)))
    # Old in-flight Video Indexer work cannot be polled by the new worker.
    op.execute("UPDATE assets SET analysis_state = 'queued', analysis_progress = NULL "
               "WHERE type = 'video' AND analysis_state IN ('sending', 'indexing', 'preparing')")
    op.drop_column("assets", "azure_video_id")


def downgrade() -> None:
    op.add_column("assets", sa.Column("azure_video_id", sa.String(100)))
    op.drop_column("assets", "tag_error")
    op.drop_column("assets", "speech_audio_blob")
    op.drop_column("assets", "speech_job_url")
