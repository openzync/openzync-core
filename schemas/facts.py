"""Pydantic schemas for the facts (business data ingestion) domain.

Schemas must never import from ``models/``, ``services/``, or ``routers/``.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    """Request body for ``POST /v1/projects/{project_id}/facts``.

    Attributes:
        session_id: Required session external ID the facts are associated
            with. The session must exist — it is never auto-created.
        facts: List of fact triples. Must contain at least 1 and at most
            500 triples.
    """

    session_id: str = Field(
        ...,
        description="Session external ID the facts are associated with. "
        "The session must exist — it is never auto-created.",
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
        superseded_count: Number of previously-active facts invalidated by
            supersession (``valid_to`` set) because a conflicting fact in
            this batch replaced them.  Zero when no conflicts occurred.
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
    superseded_count: int = Field(
        default=0,
        ge=0,
        description="Number of previously-active facts invalidated by "
        "supersession in this batch.",
    )
    status: str = Field(
        default="accepted",
        description="Always 'accepted' for synchronous acknowledgement.",
    )
    message: str = Field(
        default="Facts accepted for processing.",
        description="Human-readable status message.",
    )


class FactResponse(BaseModel):
    """A single extracted fact, returned from list endpoints.

    Attributes:
        id: Internal fact UUID.
        content: Human-readable fact statement.
        subject: Subject entity name.
        predicate: Relationship verb.
        object: Object entity name.
        confidence: Extraction confidence score (0.0–1.0).
        source_episode_id: Optional FK to the source episode.
        subject_type: Entity type of the subject (``"literal"`` or
            ``"entity"`` when resolved to ``graph_entities``).
        object_type: Entity type of the object (``"literal"`` or
            ``"entity"`` when resolved to ``graph_entities``).
        subject_entity_id: FK to ``graph_entities`` if the subject was
            resolved during extraction.
        object_entity_id: FK to ``graph_entities`` if the object was
            resolved during extraction.
        valid_from: Temporal validity start (UTC). Facts are effective
            from this instant onward; superseding facts set it to the
            supersession time.
        valid_to: Temporal validity end (UTC). Set to the supersession
            time when a conflicting fact replaces this one; ``None``
            while the fact is current.
        invalid_at: Hard-retraction timestamp (UTC) from the GDPR/memory
            wipe path; ``None`` unless the fact was explicitly
            invalidated.
        created_at: Fact creation timestamp.
    """

    id: UUID = Field(..., description="Internal fact UUID.")
    content: str = Field(..., description="Human-readable fact statement.")
    subject: str | None = Field(None, description="Subject entity name.")
    predicate: str | None = Field(None, description="Relationship verb.")
    object: str | None = Field(None, description="Object entity name.")
    confidence: float = Field(
        default=1.0, ge=0.0, le=1.0, description="Extraction confidence (0.0–1.0)."
    )
    source_episode_id: UUID | None = Field(
        None, description="Optional FK to the source episode."
    )
    subject_type: str = Field(
        default="literal",
        description="Entity type of the subject ('literal' or 'entity').",
    )
    object_type: str = Field(
        default="literal",
        description="Entity type of the object ('literal' or 'entity').",
    )
    subject_entity_id: UUID | None = Field(
        default=None,
        description="FK to graph_entities if the subject was resolved to an entity.",
    )
    object_entity_id: UUID | None = Field(
        default=None,
        description="FK to graph_entities if the object was resolved to an entity.",
    )
    valid_from: datetime | None = Field(
        None, description="Temporal validity start (UTC)."
    )
    valid_to: datetime | None = Field(
        None,
        description="Temporal validity end (UTC). Set when a conflicting "
        "fact supersedes this one; None while current.",
    )
    invalid_at: datetime | None = Field(
        None,
        description="Hard-retraction timestamp (UTC) from the wipe path; "
        "None unless explicitly invalidated.",
    )
    created_at: datetime = Field(
        ..., description="Fact creation timestamp (UTC)."
    )

    model_config = ConfigDict(from_attributes=True)


class PaginatedFactsResponse(BaseModel):
    """Paginated response for the facts list endpoint.

    Attributes:
        data: List of fact responses for the current page.
        next_cursor: Opaque cursor for the next page, or None.
        has_more: Whether additional pages exist.
    """

    data: list[FactResponse] = Field(
        ..., description="List of facts for the current page."
    )
    next_cursor: str | None = Field(
        None, description="Opaque cursor for the next page."
    )
    has_more: bool = Field(
        default=False, description="Whether additional pages exist."
    )


class FactRetractRequest(BaseModel):
    """Request body for the fact-retraction endpoint.

    Attributes:
        reason: Optional human-readable explanation of the retraction.
    """

    reason: str | None = Field(
        default=None,
        description="Optional human-readable reason for the retraction.",
    )


class FactHistoryEvent(BaseModel):
    """One invalidation-lineage event for a fact.

    UUID fields are strings, matching the repository's history dict
    convention.

    Attributes:
        id: Event row UUID (string).
        old_fact_id: The fact that stopped being current.
        new_fact_id: The fact that replaced it (``None`` for retractions).
        kind: Invalidation kind (``superseded``, ``retracted``, ...).
        reason: Optional explanation of the invalidation.
        at_time: Instant the invalidation took effect.
        source_episode_id: Episode that drove the invalidation, if any.
    """

    id: str = Field(..., description="Event UUID (string).")
    old_fact_id: str | None = Field(
        None, description="The fact that stopped being current."
    )
    new_fact_id: str | None = Field(
        None, description="The fact that replaced it, if any."
    )
    kind: str = Field(..., description="Invalidation kind.")
    reason: str | None = Field(
        None, description="Optional explanation of the invalidation."
    )
    at_time: datetime = Field(
        ..., description="Instant the invalidation took effect (UTC)."
    )
    source_episode_id: str | None = Field(
        None, description="Episode that drove the invalidation, if any."
    )


class FactHistoryResponse(BaseModel):
    """A fact plus its invalidation-lineage events.

    Attributes:
        fact: The fact whose lineage this is.
        events: Invalidation events, newest first.
    """

    fact: FactResponse = Field(
        ..., description="The fact whose lineage this is."
    )
    events: list[FactHistoryEvent] = Field(
        ..., description="Invalidation-lineage events, newest first."
    )
