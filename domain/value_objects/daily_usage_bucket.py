"""Daily metering bucket value object — one sparse UTC-day aggregate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class DailyUsageBucket:
    """One sparse UTC-day aggregate over the filtered window."""

    day: datetime
    total_tokens: int = 0
    calls: int = 0
