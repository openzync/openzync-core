"""Metering aggregate value object — summed totals over a filtered window."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UsageSummary:
    """Aggregate metering totals over a filtered window."""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    avg_duration_ms: float = 0.0
