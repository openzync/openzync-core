"""Pydantic schemas for the memory (message ingestion) domain.

Schemas must never import from ``models/``, ``services/``, or ``routers/``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "BlobMetadata",
    "BlobResponse",
    "DeleteMemoryResponse",
    "IngestMemoryRequest",
    "IngestMemoryResponse",
    "Message",
]


class BlobMetadata(BaseModel):
    """Metadata referencing an uploaded blob in a multipart request.

    Attributes:
        blob_id: Index into the uploaded blobs list (blob_<id> field).
        mime_type: Client-declared MIME type.
        file_name: Original filename.
    """

    blob_id: int = Field(
        ..., ge=0, description="Index into the uploaded blobs list (blob_<id> field).",
    )
    mime_type: str = Field(
        ..., max_length=128, description="Client-declared MIME type.",
    )
    file_name: str = Field(
        ..., max_length=512, description="Original filename.",
    )

    @field_validator("mime_type")
    @classmethod
    def check_mime_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("mime_type must not be empty")
        return v.strip().lower()


class Message(BaseModel):
    """A single conversation turn within a session.

    Attributes:
        role: Message sender role — one of ``user``, ``assistant``,
            ``system``, ``tool``.
        content: Message body text. Maximum 64KB when UTF-8 encoded.
        created_at: ISO-8601 timestamp. Assigned server-side if omitted.
        metadata: Optional caller-defined metadata (tags, labels, etc.).
    """

    role: str = Field(
        ...,
        description="Message sender role. One of: user, assistant, system, tool.",
        pattern=r"^(user|assistant|system|tool)$",
    )
    content: str = Field(
        ...,
        description="Message body text. Max 64KB.",
        max_length=65536,
    )
    created_at: datetime | None = Field(
        default=None,
        description="ISO-8601 timestamp of when the message was created. "
        "Assigned server-side if omitted.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional caller-defined metadata (tags, labels, etc.).",
    )
    blobs: list[BlobMetadata] = Field(
        default_factory=list,
        description="Attached binary files (images, PDFs, etc.). References are "
        "indexed by position in the multipart upload.",
    )

    @field_validator("content")
    @classmethod
    def check_at_least_one_or_bytesize(cls, v: str, info: Any) -> str:
        """Allow empty content when blobs are present; otherwise validate size.

        ``max_length`` on the ``Field`` only checks Unicode code-point count.
        Multi-byte characters (e.g. emoji) can blow past 64KB on the wire
        while staying under the character limit. This validator catches that.

        Args:
            v: The content string to validate.
            info: Validation info — used to check whether blobs are present.

        Returns:
            The content string unchanged if valid.

        Raises:
            ValueError: If content is empty AND no blobs are present, or
                if the UTF-8 encoded content exceeds 65536 bytes.
        """
        blobs = info.data.get("blobs", [])
        if not v and not blobs:
            raise ValueError("Either content or at least one blob must be provided")
        if v and len(v.encode("utf-8")) > 65536:
            raise ValueError("Content exceeds 64KB when encoded as UTF-8")
        return v


class IngestMemoryRequest(BaseModel):
    """Request body for ``POST /v1/projects/{project_id}/memory``.

    Attributes:
        session_id: Required session external ID. The session must exist
            (created via ``POST /sessions``) — it is never auto-created.
        messages: List of message objects. Must contain at least 1 and
            at most 1000 messages.
    """

    session_id: str = Field(
        ...,
        description="Session external_id. The session must exist — it is "
        "never auto-created.",
    )
    messages: list[Message] = Field(
        ...,
        description="List of message objects to ingest. At least 1 required.",
        min_length=1,
        max_length=1000,
    )


class IngestMemoryResponse(BaseModel):
    """Response returned after successful ingestion.

    Attributes:
        job_id: UUID string identifying the async enrichment job. Can be
            used to track completion via the job status endpoint.
        episode_count: Number of episodes (messages) ingested.
        status: Always ``"accepted"`` for synchronous acknowledgement.
        message: Human-readable status message.
    """

    job_id: str | None = Field(
        default=None,
        description="UUID of the async enrichment job for tracking.",
    )
    episode_count: int = Field(
        default=0,
        description="Number of episodes (messages) ingested.",
    )
    blob_count: int = Field(
        default=0,
        description="Number of blobs (file attachments) ingested.",
    )
    status: str = Field(
        default="accepted",
        description="Always 'accepted' for synchronous acknowledgement.",
    )
    message: str = Field(
        default="Messages accepted for processing",
        description="Human-readable status message.",
    )


# TODO(blob-get-endpoint): Add a GET /v1/projects/{id}/memory/blobs/{blob_id}
# endpoint that uses this schema. Currently scaffold-only.
class BlobResponse(BaseModel):
    """Response model for a single blob returned by GET endpoints.

    Attributes:
        id: UUID of the blob record.
        file_name: Original filename from the upload.
        mime_type: MIME type (e.g. ``"application/pdf"``).
        file_size: Size in bytes.
        storage_url: Signed/redirect URL for direct S3 download (may be null).
        width: Image width in pixels (null for non-images).
        height: Image height in pixels (null for non-images).
        blob_index: Positional index within the episode's blobs (0-based).
    """

    id: UUID
    file_name: str
    mime_type: str
    file_size: int
    storage_url: str | None = None
    download_url: str | None = None  # Presigned URL, short TTL
    width: int | None = None
    height: int | None = None
    blob_index: int = 0

    model_config = ConfigDict(from_attributes=True)


class DeleteMemoryResponse(BaseModel):
    """Response body for ``DELETE /v1/projects/{project_id}/memory``.

    Attributes:
        status: Outcome of the deletion operation.
        episodes_deleted: Number of episodes soft-deleted.
        facts_deleted: Number of facts soft-deleted.
    """

    status: str = Field(
        default="deleted",
        description="Outcome of the deletion operation.",
    )
    episodes_deleted: int = Field(
        default=0,
        ge=0,
        description="Number of episodes soft-deleted.",
    )
    facts_deleted: int = Field(
        default=0,
        ge=0,
        description="Number of facts soft-deleted.",
    )
