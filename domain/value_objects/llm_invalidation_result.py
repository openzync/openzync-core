"""LLM invalidation outcome — counts from one contradiction close-out pass."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LLMInvalidationResult:
    """Outcome of one LLM-driven invalidation pass.

    Attributes:
        closed_count: Number of facts actually closed (``valid_to`` set).
        skipped_count: Number of facts already closed — the overlap
            guard skipped them because the deterministic layer
            (supersession/retraction) may have won first.  Not an error.
    """

    closed_count: int
    skipped_count: int
