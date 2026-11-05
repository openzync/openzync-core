"""LLM-driven invalidation — contradiction detection close-out."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import UUID

from core.events import EventType
from core.exceptions import ValidationError
from models.fact import Fact
from services.facts.post_commit_effects import (
    _inc_invalidated_total,
    _inc_retracted_total,
    _purge_project_context,
)
from services.graph_edge_sync_service import make_supersession_event

if TYPE_CHECKING:
    from services.graph_edge_sync_service import SupersessionEvent

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class LLMInvalidationResult:
    """Outcome of one LLM-driven invalidation pass.

    Attributes:
        closed_count: Number of facts actually closed (``valid_to`` set).
        skipped_count: Number of facts already closed — the overlap
            guard skipped them because the deterministic layer
            (supersession/retraction) may have won first.  Not an error.
    """

    closed_count: int
    skipped_count: int

class LLMInvalidationMixin:
    """Composed into :class:`FactInvalidationService`."""

    _db: Any
    _fact_repo: Any
    _webhook_service: Any
    _cache_service: Any
    _graph_sync: Any

    async def apply_llm_invalidations(
        self,
        *,
        org_id: UUID,
        project_id: UUID,
        invalidations: list[dict[str, Any]],
        ref_to_fact: dict[str, Fact],
        successor_by_ref: dict[str, Fact | None],
        now: datetime | None = None,
    ) -> LLMInvalidationResult:
        """Close facts invalidated by LLM-driven contradiction detection.

        Phase 2 core path: the LLM layer detected contradictions against
        candidate facts and hands back explicit invalidations, each
        naming a candidate (``existing_fact_ref``), an optional inserted
        successor (``successor_fact_ref``), and a reason.  Every close
        here runs in the caller's open transaction — never committed by
        this method, matching :meth:`ingest_with_supersession`.

        Trust boundary (architect rule): every ``existing_fact_ref`` MUST
        resolve in ``ref_to_fact`` — an unknown reference is a loud
        ``ValidationError``, never a silent skip.

        Concurrency: an advisory xact lock per targeted fact serialises
        concurrent invalidators on the same fact (the loser's re-scan
        after the winner commits sees ``valid_to`` closed and skips).

        Overlap guard: a candidate already closed by the deterministic
        layer — ``valid_to`` set (supersession) or ``invalid_at`` set
        (retraction) — is skipped and counted, never double-fired.

        Post-commit effects (queued like the supersession path):
        ``FACT_INVALIDATED`` webhook, project context-cache purge,
        ``facts_invalidated_total`` bump, and graph edge-sync via the D1
        rule — the successor's real edge key decides whether the old edge
        is expired (case 1/2) or kept (case 3 re-assertion).

        Args:
            org_id: Tenant scope.
            project_id: Project scope.
            invalidations: List of dicts with ``existing_fact_ref``
                (required), ``successor_fact_ref`` (optional), ``reason``
                (optional).
            ref_to_fact: Prompt-reference → active candidate ``Fact`` map
                (the set the LLM was allowed to invalidate).
            successor_by_ref: Prompt-reference → inserted successor
                ``Fact`` (or ``None`` for a successor-less invalidation).
            now: Invalidation instant; defaults to the current UTC time.

        Returns:
            An :class:`LLMInvalidationResult` with the closed/skipped
            counts.

        Raises:
            ValidationError: If any ``existing_fact_ref`` is not present
                in ``ref_to_fact``, or any ``successor_fact_ref`` is not
                present in ``successor_by_ref``.
        """
        from datetime import UTC

        now = now or datetime.now(UTC)
        if not invalidations:
            return LLMInvalidationResult(closed_count=0, skipped_count=0)

        # Trust boundary — resolve every reference before touching the DB.
        for inv in invalidations:
            ref = inv.get("existing_fact_ref")
            if ref not in ref_to_fact:
                raise ValidationError(
                    f"Unknown fact reference {ref!r} in LLM invalidation — "
                    "it must be one of the provided candidate facts."
                )
            successor_ref = inv.get("successor_fact_ref")
            if successor_ref is not None and successor_ref not in successor_by_ref:
                raise ValidationError(
                    f"Unknown successor fact reference {successor_ref!r} in "
                    "LLM invalidation — it must be one of the provided "
                    "successor facts."
                )

        # Serialize concurrent invalidators per targeted fact (sorted,
        # deduped acquisition keeps multi-fact passes deadlock-free).
        targeted_ids = {
            ref_to_fact[inv["existing_fact_ref"]].id for inv in invalidations
        }
        lock_keys = sorted(
            f"inv:{org_id}:{project_id}:{fact_id}" for fact_id in targeted_ids
        )
        if lock_keys:
            await self._fact_repo.lock_conflict_identities(lock_keys)

        closed_count = 0
        skipped_count = 0
        events: list[SupersessionEvent] = []
        reason_by_old_fact: dict[UUID, str | None] = {}
        for inv in invalidations:
            old = ref_to_fact[inv["existing_fact_ref"]]
            # Overlap guard — already closed: the deterministic layer
            # (supersession closes valid_to, retraction closes invalid_at)
            # may have won the race.  Skip, do not raise — double-firing
            # an invalidation is the bug to trap.  getattr keeps duck-typed
            # stand-ins (unit-test doubles) working — real Facts always
            # carry both attributes.
            if (
                old.valid_to is not None
                or getattr(old, "invalid_at", None) is not None
            ):
                skipped_count += 1
                continue
            successor_ref = inv.get("successor_fact_ref")
            successor = successor_by_ref[successor_ref] if successor_ref else None
            await self._fact_repo.set_valid_to(old.id, now)
            await self._fact_repo.set_superseded_by(
                old.id, successor.id if successor is not None else None
            )
            await self._fact_repo.record_invalidation_event(
                organization_id=org_id,
                project_id=project_id,
                old_fact_id=old.id,
                new_fact_id=successor.id if successor is not None else None,
                kind="llm_invalidated",
                reason=inv.get("reason") or None,
                at_time=now,
            )
            closed_count += 1
            reason_by_old_fact[old.id] = inv.get("reason") or None
            # D1-rule event against the REAL successor row (never a stub) —
            # the same-key re-assertion flag decides edge expiry post-commit.
            events.append(
                make_supersession_event(
                    old_fact=old,
                    new_fact=successor,
                    triple={
                        "subject": str(old.subject or ""),
                        "predicate": str(old.predicate or ""),
                        "object": str(old.object or ""),
                    },
                )
            )

        # ── Post-commit effects (deferred to the real commit) ──────────────
        if closed_count:
            self._queue_post_commit_effect(
                lambda: _inc_invalidated_total(closed_count)
            )
            if self._cache_service is not None:
                self._queue_post_commit_effect(
                    partial(
                        _purge_project_context,
                        self._cache_service,
                        org_id,
                        project_id,
                    )
                )
        if events and self._webhook_service is not None:
            for transition in events:
                self._queue_post_commit_effect(
                    self._make_webhook_effect(
                        org_id=org_id,
                        project_id=project_id,
                        old_id=transition.old_fact_id,
                        new_id=transition.new_fact_id,
                        triple=transition.triple,
                        event_type=EventType.FACT_INVALIDATED,
                        reason=reason_by_old_fact.get(transition.old_fact_id),
                    )
                )
        if events and self._graph_sync is not None:
            self._queue_post_commit_effect(
                self._make_graph_sync_effect(
                    org_id=org_id,
                    project_id=project_id,
                    events=events,
                    at_time=now,
                )
            )

        logger.info(
            "fact_invalidation.llm_applied",
            extra={
                "org_id": str(org_id),
                "project_id": str(project_id),
                "closed_count": closed_count,
                "skipped_count": skipped_count,
            },
        )
        return LLMInvalidationResult(
            closed_count=closed_count, skipped_count=skipped_count
        )

    def notify_retraction(
        self,
        *,
        org_id: UUID,
        project_id: UUID,
        old_fact: object,
        at_time: datetime,
    ) -> None:
        """Queue the post-commit effects for a manually retracted fact.

        Routes ``invalid_at`` retraction paths through the same
        post-commit effects as supersession.  A retracted fact has no
        successor, so the D1 rule expires any edge it asserted
        (case 1 — no successor re-asserts the fact); the webhook carries
        ``new_fact_id=None`` to signal the retraction.

        Effects (each gated on its collaborator, except the metric which
        is module-level): graph edge-sync, ``FACT_RETRACTED`` webhook,
        project context-cache purge, ``facts_retracted_total`` bump.

        Args:
            org_id: Tenant scope.
            project_id: Project scope.
            old_fact: The retracted fact — entity ID attributes are read
                for the edge key.
            at_time: The retraction instant (deterministic, never a fresh
                clock read).
        """
        events = [make_supersession_event(old_fact, None, {})]
        if self._graph_sync is not None:
            self._queue_post_commit_effect(
                self._make_graph_sync_effect(
                    org_id=org_id,
                    project_id=project_id,
                    events=events,
                    at_time=at_time,
                )
            )
        if self._webhook_service is not None:
            # Single retraction → events[0] is the one transition; its
            # id was already validated by make_supersession_event.
            self._queue_post_commit_effect(
                self._make_webhook_effect(
                    org_id=org_id,
                    project_id=project_id,
                    old_id=events[0].old_fact_id,
                    new_id=None,
                    triple={
                        "subject": str(getattr(old_fact, "subject", None) or ""),
                        "predicate": str(getattr(old_fact, "predicate", None) or ""),
                        "object": str(getattr(old_fact, "object", None) or ""),
                    },
                    event_type=EventType.FACT_RETRACTED,
                )
            )
        if self._cache_service is not None:
            self._queue_post_commit_effect(
                partial(
                    _purge_project_context,
                    self._cache_service,
                    org_id,
                    project_id,
                )
            )
        self._queue_post_commit_effect(lambda: _inc_retracted_total(1))
