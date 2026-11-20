"""LLM usage model — append-only record of every metered inference call.

This table tracks token consumption and latency per inference call for
operational observability. It is **immutable** — rows are inserted once
and never modified. The ``total_tokens`` column is a generated column
computed as ``prompt_tokens + completion_tokens``.
"""

import uuid

from sqlalchemy import Computed, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, CreatedAtMixin


class LLMUsage(CreatedAtMixin, Base):
    """A single metered LLM inference usage record.

    Attributes:
        id: UUID primary key.
        user_id: Owning user (denormalized for fast
            aggregation queries).
        provider: Backend identifier (e.g. ``openai``, ``ollama``).
        model: Model identifier (e.g., ``gpt-4o``, ``claude-sonnet-4``).
        task_type: Task vocabulary label (e.g., ``enrich_episode``).
        worker: Worker/service that issued the call (e.g.
            ``enrich_episode``, ``query_embed``, ``schema_preview``).
        project_id: Project scope, when known.
        episode_id: Source episode, when the call enriches one.
        session_id: Source session, when known.
        community_id: Community being summarised, when applicable.
        task_run_id: Owning task run, when applicable.
        prompt_tokens: Number of tokens in the prompt.
        completion_tokens: Number of tokens in the completion.
        reasoning_tokens: Reasoning tokens reported separately by the
            provider (folded into ``completion_tokens`` upstream, so
            excluded from ``total_tokens``).
        cache_read_input_tokens: Tokens served from the provider's prompt
            cache (e.g. Anthropic ``cache_read_input_tokens``).
        cache_creation_input_tokens: Tokens written to the provider's prompt
            cache (e.g. Anthropic ``cache_creation_input_tokens``).
        total_tokens: **Generated column** — always equals
            ``prompt_tokens + completion_tokens``. Computed and stored
            by PostgreSQL; cannot be written directly.
        embed_count: Number of embedded texts (embed calls only).
        embed_dim: Embedding dimensionality (embed calls only).
        idempotency_key: Deduplication key — inserts conflict-do-nothing
            on this column.
        duration_ms: Wall-clock duration of the inference call in
            milliseconds.
        created_at: Immutable timestamp (inherited from ``CreatedAtMixin``).
    """

    __tablename__ = "llm_usage"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    provider: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    model: Mapped[str] = mapped_column(Text, nullable=False)
    task_type: Mapped[str] = mapped_column(Text, nullable=False)
    worker: Mapped[str | None] = mapped_column(Text, nullable=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    episode_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    community_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    task_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    prompt_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    completion_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    reasoning_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    cache_read_input_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    cache_creation_input_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    total_tokens: Mapped[int] = mapped_column(
        Integer,
        # persisted=True is explicit, not decorative: without it PostgreSQL 18+
        # renders VIRTUAL instead of STORED, which would silently change the
        # column from stored to recomputed. Matches the DDL in migrations
        # 0001/0004, which create this column STORED on every current PG.
        Computed("prompt_tokens + completion_tokens", persisted=True),
        nullable=False,
    )
    embed_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embed_dim: Mapped[int | None] = mapped_column(Integer, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    duration_ms: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    __table_args__ = (
        Index("ix_llm_usage_org_created", "user_id", "created_at"),
    )

    def __repr__(self) -> str:
        """Return a debug-friendly repr with id, model, and token totals."""
        return (
            f"<LLMUsage id={self.id} model={self.model!r} "
            f"total_tokens={self.total_tokens}>"
        )
