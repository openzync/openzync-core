"""Episode domain entity — one message turn within a session."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Episode:
    """Framework-free episode snapshot mapped from the ORM row at the boundary.

    Attributes:
        id: Episode primary key.
        session_id: Parent session.
        project_id: Owning project (tenant scope).
        organization_id: Owning organization (tenant scope).
        user_id: Attributed user.
        role: Message role (``user`` / ``assistant`` / ``system`` / ``tool``).
        content: Redacted message body.
        sequence_number: Order within the session.
        created_at: Row creation timestamp.
    """

    id: UUID
    session_id: UUID
    project_id: UUID
    organization_id: UUID
    user_id: UUID
    role: str
    content: str
    sequence_number: int
    created_at: datetime | None = None
