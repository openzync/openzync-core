"""Graph edge key value object — triple key derived from a fact."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class EdgeKey:
    """Triple key of a graph edge derived from a fact.

    ``source_id`` is the fact's subject entity, ``target_id`` the fact's
    object entity, ``relationship_type`` the predicate.  A fact only
    yields an edge key when BOTH entity IDs resolve — literal-subject
    facts never create edges.
    """

    source_id: UUID
    target_id: UUID
    relationship_type: str
