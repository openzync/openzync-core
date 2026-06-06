"""Pydantic schemas for the facts (business data ingestion) domain.

Schemas must never import from ``models/``, ``services/``, or ``routers/``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class FactTriple(BaseModel):
    """A single fact triple for batch ingestion.

    Attributes:
        subject: The subject entity name (e.g. ``"Alice"``).
        predicate: The relationship verb (e.g. ``"likes"``, ``"works_at"``).
        object: The object entity name (e.g. ``"hiking"``, ``"Acme Corp"``).
        content: Optional human-readable fact statement. Auto-generated from
            subject-predicate-object if omitted.
        confidence: Extraction confidence score (0.0–1.0). Defaults to 1.0.
    """

    subject: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Subject entity name.",
    )
    predicate: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Relationship verb (e.g. 'likes', 'works_at').",
    )
    object: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Object entity name.",
    )
    content: str | None = Field(
        default=None,
        description="Human-readable fact statement. Auto-generated if omitted.",
    )
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Extraction confidence score (0.0–1.0).",
    )

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, v: float) -> float:
        """Ensure confidence is in the valid range (0.0–1.0)."""
        return max(0.0, min(1.0, v))


class FactBatchRequest(BaseModel):
    """Request body for ``POST /v1/users/{user_id}/facts``.

    Attributes:
        session_id: Optional session external ID to associate facts with.
            If omitted, facts are not linked to any session.
        facts: List of fact triples. Must contain at least 1 and at most
            500 triples.
    """

    session_id: str | None = Field(
        default=None,
        description="Optional session external ID to associate facts with.",
    )
    facts: list[FactTriple] = Field(
        ...,
        description="List of fact triples to ingest.",
        min_length=1,
        max_length=500,
    )


class FactBatchResponse(BaseModel):
    """Response returned after successful fact batch ingestion.

    Attributes:
        job_id: UUID string identifying the async enrichment job.
        accepted_count: Number of facts accepted for processing.
        status: Always ``"accepted"`` for synchronous acknowledgement.
        message: Human-readable status message.
    """

    job_id: str = Field(
        ...,
        description="UUID of the async enrichment job for tracking.",
    )
    accepted_count: int = Field(
        ...,
        ge=0,
        description="Number of facts accepted for processing.",
    )
    status: str = Field(
        default="accepted",
        description="Always 'accepted' for synchronous acknowledgement.",
    )
    message: str = Field(
        default="Facts accepted for processing.",
        description="Human-readable status message.",
    )
