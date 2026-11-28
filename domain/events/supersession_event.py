"""Supersession transition event — one old-fact → successor transition."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from domain.value_objects.edge_key import EdgeKey


@dataclass(frozen=True, slots=True)
class SupersessionEvent:
    """One old-fact → successor transition recorded during supersession.

    Attributes:
        old_fact_id: The superseded fact's ID.
        new_fact_id: The successor fact's ID, or ``None`` for a
            retraction (case 1 — no successor re-asserts the fact).
        triple: The SPO strings (payload provenance for webhooks/logs).
        old_edge_key: Edge key of the superseded fact, or ``None`` when
            either entity ID is unresolved (literal → no edge exists).
        successor_reasserts: ``True`` when the successor asserts the
            SAME edge key (case 3 — the edge survives, nothing to expire).
    """

    old_fact_id: UUID
    new_fact_id: UUID | None
    triple: dict[str, str] = field(default_factory=dict)
    old_edge_key: EdgeKey | None = None
    successor_reasserts: bool = False
