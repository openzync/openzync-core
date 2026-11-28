"""Supersession batch outcome — counts plus the inserted rows for the caller."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class FactIngestionResult:
    """Outcome of one supersession-aware batch ingestion.

    Attributes:
        created: The inserted fact rows (new facts, not the
            superseded ones).  Typed as ``Any`` because the ORM model
            lives in the infrastructure layer and must not leak into
            domain — callers treat these as opaque inserted rows.
        inserted_count: Number of rows actually inserted.
        superseded_count: Number of previously-active facts closed by
            supersession (``valid_to`` set).
        skipped_count: Number of incoming facts skipped because a
            conflicting fact (existing or already processed in this
            batch) already had identical content.
    """

    created: list[Any] = field(default_factory=list)
    inserted_count: int = 0
    superseded_count: int = 0
    skipped_count: int = 0
