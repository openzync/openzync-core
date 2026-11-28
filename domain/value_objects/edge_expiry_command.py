"""Graph edge expiry command — one triple key plus the instant to expire."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class EdgeExpiryCommand:
    """One edge-expiry unit: the triple key + the deterministic instant."""

    user_id: UUID
    project_id: UUID
    source_id: UUID
    target_id: UUID
    relationship_type: str
    at_time: datetime
    fact_id: UUID
