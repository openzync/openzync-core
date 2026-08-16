"""LLM usage recording — append-only rows for every inference call.

A single insert helper: the caller's transaction owns the commit, so the
usage row commits atomically with the surrounding work.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from models.llm_usage import LLMUsage

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from core.llm import TokenUsage


async def record_llm_usage(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    model: str,
    task_type: str,
    usage: TokenUsage,
    duration_ms: int,
    cost_estimate: Decimal = Decimal("0"),
) -> None:
    """Record a single LLM inference call in the append-only ``llm_usage`` table.

    The row is added to *session* and flushed but **not** committed — the
    caller's transaction owns the commit, so the usage row commits
    atomically with the surrounding work.

    Args:
        session: Active async session (RLS-scoped to the org).
        organization_id: Owning organization UUID.
        model: Model identifier reported by the backend (``response.model``).
        task_type: Task vocabulary label (e.g. ``"enrich_episode"``).
        usage: Token usage reported by the backend.
        duration_ms: Wall-clock duration of the inference call.
        cost_estimate: Estimated cost in USD (defaults to 0).

    Raises:
        Exception: Any DB error propagates — usage recording must never be
            silently swallowed.
    """
    session.add(
        LLMUsage(
            organization_id=organization_id,
            model=model,
            task_type=task_type,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cache_read_input_tokens=usage.cache_read_input_tokens,
            cache_creation_input_tokens=usage.cache_creation_input_tokens,
            cost_estimate=cost_estimate,
            duration_ms=duration_ms,
        )
    )
    await session.flush()
