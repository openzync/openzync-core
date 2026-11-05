"""Fact domain entity — one subject-predicate-object triple."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Fact:
    """Framework-free fact snapshot mapped from the ORM row at the boundary.

    Attributes:
        id: Fact primary key.
        project_id: Owning project (tenant scope).
        organization_id: Owning organization (tenant scope).
        user_id: Attributed user.
        subject: Triple subject surface form.
        predicate: Triple predicate surface form.
        object: Triple object surface form.
        content: Human-readable statement.
        confidence: Extraction confidence (0.0–1.0).
        valid_from: Temporal validity start.
        valid_to: Temporal validity end (``None`` = open).
    """

    id: UUID
    project_id: UUID
    organization_id: UUID
    user_id: UUID
    subject: str
    predicate: str
    object: str
    content: str
    confidence: float = 1.0
    valid_from: datetime | None = None
    valid_to: datetime | None = None
