"""Temporal validation service — warn-only consistency checks for facts.

All methods in this service are **warn-only** — they log structured warnings
and return results but **never mutate data**.  This is a deliberate design
decision for Phase 3: we gather data on temporal anomalies before deciding
whether auto-mutation (auto-expiry, range correction) should be enabled
behind a feature flag in a later phase.

Key validations:

1. **Cross-episode overlap** — facts with the same ``(subject, predicate,
   object)`` but different ``source_episode_id`` whose ``tstzrange`` values
   overlap.  The exclusion constraint ``uq_facts_temporal_excl`` only
   prevents overlaps within the same episode, so cross-episode duplicates
   can still accumulate.

2. **Invalid ranges** — facts where ``valid_to < valid_from`` (logically
   impossible).

3. **Future-dated facts** — facts whose ``valid_from`` is more than 24 hours
   in the future, indicating a possible data-pipeline issue.

4. **Pre-insert batch validation** — check an incoming fact batch for
   self-overlapping triples before they reach the database.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from repositories.fact_repository import FactRepository

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

MAX_FUTURE_HOURS: int = 24
"""Maximum allowed hours for ``valid_from`` to be in the future before
a warning is raised."""


# ── Warning data class ─────────────────────────────────────────────────────────


class TemporalWarning:
    """A single temporal-consistency warning.

    Attributes:
        code: Machine-readable warning code (e.g. ``"overlap"``,
            ``"invalid_range"``, ``"future_date"``).
        message: Human-readable description.
        detail: Structured context for log aggregation.
    """

    __slots__ = ("code", "message", "detail")

    def __init__(
        self,
        code: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for API responses or logging."""
        return {
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
        }


# ── Service ────────────────────────────────────────────────────────────────────


class TemporalValidationService:
    """Warn-only temporal consistency checks for facts.

    Args:
        fact_repo: Repository for fact database access.
    """

    def __init__(self, fact_repo: FactRepository) -> None:
        self._fact_repo = fact_repo

    # ── Public API ──────────────────────────────────────────────────────────────

    async def check_project_temporal_consistency(
        self,
        project_id: UUID,
        *,
        organization_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        """Scan all non-invalidated facts in a project for overlapping triples
        across different source episodes.

        The exclusion constraint only prevents overlaps within the same
        ``source_episode_id``.  This check finds triples that overlap but
        originate from different episodes — a sign that the extraction
        pipeline may be producing conflicting facts.

        Args:
            project_id: The project to scan.
            organization_id: Optional tenant filter for defense-in-depth.

        Returns:
            A list of warning dicts, each with ``code``, ``message``, and
            ``detail`` keys.  Empty list means no issues found.
        """
        warnings: list[dict[str, Any]] = []

        facts = await self._fact_repo.get_all_active_for_project(
            project_id=project_id,
            organization_id=organization_id,
        )

        # Group by (subject, predicate, object)
        groups: dict[tuple[str | None, str | None, str | None], list[dict]] = (
            defaultdict(list)
        )
        for fact in facts:
            key = (fact.subject, fact.predicate, fact.object)
            groups[key].append(fact)

        for triple, group in groups.items():
            if len(group) < 2:
                continue

            # Check every pair in the group for overlap
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    a, b = group[i], group[j]

                    # If either fact is from the same episode, the exclusion
                    # constraint already covers it — skip the pair.
                    if a.source_episode_id == b.source_episode_id:
                        continue

                    if self._ranges_overlap(
                        a.valid_from, a.valid_to,
                        b.valid_from, b.valid_to,
                    ):
                        subject, predicate, obj = triple
                        warnings.append(
                            TemporalWarning(
                                code="overlap",
                                message=(
                                    f"Cross-episode temporal overlap for triple "
                                    f"({subject!r}, {predicate!r}, {obj!r})"
                                ),
                                detail={
                                    "project_id": str(project_id),
                                    "subject": subject,
                                    "predicate": predicate,
                                    "object": obj,
                                    "fact_a_id": str(a.id),
                                    "fact_b_id": str(b.id),
                                    "episode_a_id": str(a.source_episode_id),
                                    "episode_b_id": str(b.source_episode_id),
                                    "range_a": (
                                        str(a.valid_from) if a.valid_from else "-inf",
                                        str(a.valid_to) if a.valid_to else "inf",
                                    ),
                                    "range_b": (
                                        str(b.valid_from) if b.valid_from else "-inf",
                                        str(b.valid_to) if b.valid_to else "inf",
                                    ),
                                },
                            ).to_dict()
                        )

        for w in warnings:
            logger.warning("temporal_validation.%s", w["code"], extra=w["detail"])

        return warnings

    async def check_fact_ranges(
        self,
        project_id: UUID,
        *,
        organization_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        """Scan all non-invalidated facts for invalid temporal ranges.

        Checks:
        - ``valid_to IS NOT NULL AND valid_to < valid_from`` — logically
          impossible range.
        - ``valid_from > now() + 24h`` — future-dated fact, indicates a
          possible data-pipeline issue.

        Args:
            project_id: The project to scan.
            organization_id: Optional tenant filter for defense-in-depth.

        Returns:
            A list of warning dicts, empty if no issues found.
        """
        warnings: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)
        future_threshold = now + timedelta(hours=MAX_FUTURE_HOURS)

        facts = await self._fact_repo.get_all_active_for_project(
            project_id=project_id,
            organization_id=organization_id,
        )

        for fact in facts:
            # Check: valid_to < valid_from
            if fact.valid_to is not None and fact.valid_from is not None:
                if fact.valid_to < fact.valid_from:
                    warnings.append(
                        TemporalWarning(
                            code="invalid_range",
                            message=(
                                f"Fact {fact.id} has valid_to ({fact.valid_to}) "
                                f"before valid_from ({fact.valid_from})"
                            ),
                            detail={
                                "fact_id": str(fact.id),
                                "project_id": str(project_id),
                                "valid_from": str(fact.valid_from),
                                "valid_to": str(fact.valid_to),
                                "subject": fact.subject,
                                "predicate": fact.predicate,
                                "object": fact.object,
                            },
                        ).to_dict()
                    )

            # Check: valid_from too far in the future
            if fact.valid_from is not None and fact.valid_from > future_threshold:
                warnings.append(
                    TemporalWarning(
                        code="future_date",
                        message=(
                            f"Fact {fact.id} has valid_from ({fact.valid_from}) "
                            f"more than {MAX_FUTURE_HOURS}h in the future"
                        ),
                        detail={
                            "fact_id": str(fact.id),
                            "project_id": str(project_id),
                            "valid_from": str(fact.valid_from),
                            "hours_ahead": round(
                                (fact.valid_from - now).total_seconds() / 3600, 1
                            ),
                            "subject": fact.subject,
                            "predicate": fact.predicate,
                            "object": fact.object,
                        },
                    ).to_dict()
                )

        for w in warnings:
            logger.warning("temporal_validation.%s", w["code"], extra=w["detail"])

        return warnings

    async def validate_batch(
        self,
        facts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Pre-insert validation for a batch of incoming facts.

        Checks for self-overlapping triples within the batch itself.
        Unlike the DB-level exclusion constraint (which compares against
        existing rows), this catches intra-batch conflicts before they
        reach PostgreSQL.

        Args:
            facts: List of fact dicts.  Each dict must have ``subject``,
                ``predicate``, ``object``, and optionally ``valid_from``,
                ``valid_to``, and ``source_episode_id`` keys.

        Returns:
            A list of warning dicts for any conflicts found.  Empty list
            means no issues.
        """
        warnings: list[dict[str, Any]] = []

        # Group by (subject, predicate, object, source_episode_id)
        groups: dict[
            tuple[str | None, str | None, str | None, str | None],
            list[dict],
        ] = defaultdict(list)

        for i, f in enumerate(facts):
            key = (
                f.get("subject"),
                f.get("predicate"),
                f.get("object"),
                str(f.get("source_episode_id"))
                if f.get("source_episode_id")
                else None,
            )
            groups[key].append({"index": i, **f})

        for key, group in groups.items():
            if len(group) < 2:
                continue

            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    a, b = group[i], group[j]
                    if self._ranges_overlap(
                        a.get("valid_from"),
                        a.get("valid_to"),
                        b.get("valid_from"),
                        b.get("valid_to"),
                    ):
                        subject, predicate, obj, episode = key
                        warnings.append(
                            TemporalWarning(
                                code="batch_overlap",
                                message=(
                                    f"Batch self-overlap for triple "
                                    f"({subject!r}, {predicate!r}, {obj!r}) "
                                    f"at indices {a['index']} and {b['index']}"
                                ),
                                detail={
                                    "index_a": a["index"],
                                    "index_b": b["index"],
                                    "subject": subject,
                                    "predicate": predicate,
                                    "object": obj,
                                    "source_episode_id": episode,
                                    "range_a": (
                                        str(a.get("valid_from"))
                                        if a.get("valid_from")
                                        else "-inf",
                                        str(a.get("valid_to"))
                                        if a.get("valid_to")
                                        else "inf",
                                    ),
                                    "range_b": (
                                        str(b.get("valid_from"))
                                        if b.get("valid_from")
                                        else "-inf",
                                        str(b.get("valid_to"))
                                        if b.get("valid_to")
                                        else "inf",
                                    ),
                                },
                            ).to_dict()
                        )

        return warnings

    # ── Internal helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _ranges_overlap(
        from_a: datetime | None,
        to_a: datetime | None,
        from_b: datetime | None,
        to_b: datetime | None,
    ) -> bool:
        """Check whether two ``'[)'`` half-open ranges overlap.

        ``None`` is treated as unbounded: ``None`` for start → ``-infinity``,
        ``None`` for end → ``infinity``.  This mirrors the exclusion
        constraint's ``COALESCE`` logic.

        ``'[)'`` semantics: [from, to) — ``to`` is **excluded**.
        """
        # Treat None as unbounded
        a_start = from_a if from_a is not None else datetime.min.replace(tzinfo=timezone.utc)
        a_end = to_a if to_a is not None else datetime.max.replace(tzinfo=timezone.utc)
        b_start = from_b if from_b is not None else datetime.min.replace(tzinfo=timezone.utc)
        b_end = to_b if to_b is not None else datetime.max.replace(tzinfo=timezone.utc)

        # [a_start, a_end) and [b_start, b_end) overlap if
        # a_start < b_end AND b_start < a_end
        return a_start < b_end and b_start < a_end
