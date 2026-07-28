"""Create episode_blobs table.

Binary file attachments linked to episodes — screenshots, rendered
diagrams, exported reports, etc.  Each blob is stored in an external
backend (S3 by default) with a content-addressed key.

Revision ID: 0039
Revises: 0028
Create Date: 2026-07-24
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0039"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "episode_blobs",
        # ── Primary key ────────────────────────────────────────────────────────
        sa.Column(
            "id", sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # ── Foreign keys ───────────────────────────────────────────────────────
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("episode_id", sa.UUID(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        # ── Storage metadata ───────────────────────────────────────────────────
        sa.Column(
            "storage_backend", sa.VARCHAR(16),
            nullable=False, server_default=sa.text("'s3'"),
        ),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("file_name", sa.VARCHAR(512), nullable=False),
        sa.Column("mime_type", sa.VARCHAR(128), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("content_hash", sa.VARCHAR(64), nullable=False),
        # ── Optional media dimensions ──────────────────────────────────────────
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        # ── Extracted text (OCR / transcription) ───────────────────────────────
        sa.Column("extracted_text", sa.Text(), nullable=True),
        # ── Order within the episode ───────────────────────────────────────────
        sa.Column(
            "blob_index", sa.Integer(),
            nullable=False, server_default=sa.text("0"),
        ),
        # ── Timestamps ─────────────────────────────────────────────────────────
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        # ── Constraints ────────────────────────────────────────────────────────
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["episode_id"], ["episodes.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "episode_id", "blob_index",
            name="uq_episode_blob_index",
        ),
        # ── Table comment ──────────────────────────────────────────────────────
        comment="Binary file attachments linked to episodes",
    )

    # ── Indexes for common query patterns ──────────────────────────────────────
    # All blobs for a session (e.g. "show me everything from this session")
    op.create_index(
        "ix_episode_blobs_session", "episode_blobs", ["session_id"],
    )

    # All blobs in a project (e.g. "show me all files in this project")
    op.create_index(
        "ix_episode_blobs_project", "episode_blobs", ["project_id"],
    )

    # Lookup by content hash for dedup / reuse ("has this file been uploaded
    # before?")
    op.create_index(
        "ix_episode_blobs_content_hash", "episode_blobs", ["content_hash"],
    )


def downgrade() -> None:
    op.drop_index("ix_episode_blobs_content_hash", table_name="episode_blobs")
    op.drop_index("ix_episode_blobs_project", table_name="episode_blobs")
    op.drop_index("ix_episode_blobs_session", table_name="episode_blobs")
    op.drop_table("episode_blobs")
