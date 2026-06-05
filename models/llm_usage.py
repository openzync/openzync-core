"""LLM usage model — append-only record of every LLM inference call.

This table tracks token consumption, cost estimates, and latency per inference
call. It is **immutable** — rows are inserted once and never modified.
The ``total_tokens`` column is a generated column computed as
``prompt_tokens + completion_tokens``.
"""

import uuid
from decimal import Decimal

from sqlalchemy import Computed, Integer, Numeric, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, CreatedAtMixin


class LLMUsage(CreatedAtMixin, Base):
    """A single LLM inference usage record.

    Attributes:
        id: UUID primary key.
        organization_id: Owning organization (denormalized for fast
            aggregation queries).
        model: Model identifier (e.g., ``gpt-4o``, ``claude-sonnet-4``).
        task_type: Type of task (e.g., ``chat.completion``,
            ``embedding``, ``classification``).
        prompt_tokens: Number of tokens in the prompt.
        completion_tokens: Number of tokens in the completion.
        total_tokens: **Generated column** — always equals
            ``prompt_tokens + completion_tokens``. Computed and stored
            by PostgreSQL; cannot be written directly.
        cost_estimate: Estimated cost in USD (12 digits, 8 decimal places).
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
    organization_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    task_type: Mapped[str] = mapped_column(Text, nullable=False)
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
    total_tokens: Mapped[int] = mapped_column(
        Integer,
        Computed("prompt_tokens + completion_tokens"),
        nullable=False,
    )
    cost_estimate: Mapped[Decimal] = mapped_column(
        Numeric(12, 8),
        nullable=False,
        default=0,
        server_default="0",
    )
    duration_ms: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    def __repr__(self) -> str:
        return (
            f"<LLMUsage id={self.id} model={self.model!r} "
            f"total_tokens={self.total_tokens}>"
        )
