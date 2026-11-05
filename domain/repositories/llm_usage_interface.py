"""LLM-usage repository interface — metering write/read contracts."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol


class LLMUsageRepositoryInterface(Protocol):
    """Persistence contract for the append-only ``llm_usage`` table."""

    async def create(self, record: Any) -> None:
        """Insert a metering record, ignoring idempotency-key conflicts."""
        ...

    @classmethod
    async def create_scoped(
        cls, session_factory: Callable[[], Any], record: Any
    ) -> None:
        """Insert a metering record via an isolated RLS-scoped session."""
        ...
