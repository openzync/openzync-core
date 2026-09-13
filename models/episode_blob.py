"""EpisodeBlob model — binary file attachments linked to episodes.

Each blob is a file (image, PDF, document, etc.) attached to a message
(episode).  Blobs are stored in S3-compatible storage; this model tracks
metadata, storage location, and any text extracted from the file.
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


class EpisodeBlob(TimestampMixin, Base):
    """Binary file attachment linked to an episode (message).

    Each row represents one uploaded file.  The actual bytes are stored
    in S3-compatible storage; this table holds metadata and the path
    to retrieve them.

    Columns:
        id: UUID primary key.
        organization_id: Tenant isolation FK.
        project_id: Project-scoped FK (denormalized for query perf).
        session_id: Session FK.
        episode_id: Episode FK — the message this blob is attached to.
        created_by: User who uploaded this blob.
        storage_backend: Backend identifier (``"s3"``).
        storage_key: Full S3 object key for retrieval.
        file_name: Original filename from the upload.
        mime_type: MIME type (e.g. ``"application/pdf"``).
        file_size: Size in bytes.
        content_hash: SHA-256 hex digest of the file content (for dedup).
        width: Image width in pixels (nullable, for images only).
        height: Image height in pixels (nullable, for images only).
        extracted_text: Text extracted from the file (PDF/DOCX/TXT) or
            OCR output. Populated by the ``extract_blob_text`` worker.
        blob_index: Positional index within the episode's blobs array
            (0-based).  Unique per episode.
        created_at / updated_at: Timezone-aware timestamps from TimestampMixin.
    """

    __tablename__ = "episode_blobs"
    __table_args__ = (
        UniqueConstraint("episode_id", "blob_index", name="uq_episode_blob_index"),
        Index("ix_episode_blobs_session", "session_id"),
        Index("ix_episode_blobs_project", "project_id"),
        Index("ix_episode_blobs_content_hash", "content_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    episode_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("episodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ── Storage metadata ────────────────────────────────────────────────
    storage_backend: Mapped[str] = mapped_column(String(16), nullable=False, default="s3")
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)

    # ── File metadata ───────────────────────────────────────────────────
    file_name: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # ── Image dimensions (nullable, for images only) ────────────────────
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ── Extracted text (populated by worker) ────────────────────────────
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Position within message ─────────────────────────────────────────
    blob_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ── Timestamps from TimestampMixin (TIMESTAMP(timezone=True)) ──────

    def __repr__(self) -> str:
        return (
            f"<EpisodeBlob id={self.id} file={self.file_name} "
            f"mime={self.mime_type} episode={self.episode_id}>"
        )
