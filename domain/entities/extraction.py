"""Structured extraction domain entity — schema-driven data per episode."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Extraction:
    """Framework-free extraction snapshot mapped from the ORM row at the boundary.

    Attributes:
        id: Extraction primary key.
        project_id: Owning project (tenant scope).
        session_id: Parent session.
        episode_id: Source episode.
        schema_id: Extraction schema, or ``None`` for ad-hoc extractions.
        data: Extracted payload. Typed as an immutable-friendly mapping —
            repositories pass the stored JSON object through unchanged.
    """

    id: UUID
    project_id: UUID
    session_id: UUID
    episode_id: UUID
    schema_id: UUID | None
    data: dict[str, object]
