"""Supersession ingest — deterministic batch ingestion with conflict close-out."""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from models.fact import Fact
from services.facts.persistence import _BatchCtx, _BatchState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FactIngestionResult:
    """Outcome of one supersession-aware batch ingestion.

    Attributes:
        created: The inserted ``Fact`` ORM rows (new facts, not the
            superseded ones).
        inserted_count: Number of rows actually inserted.
        superseded_count: Number of previously-active facts closed by
            supersession (``valid_to`` set).
        skipped_count: Number of incoming facts skipped because a
            conflicting fact (existing or already processed in this
            batch) already had identical content.
    """

    created: list[Fact] = field(default_factory=list)
    inserted_count: int = 0
    superseded_count: int = 0
    skipped_count: int = 0


class SupersessionIngestMixin:
    """Deterministic supersession state machine for a fact batch."""

    _db: Any
    _fact_repo: Any
    _webhook_service: Any
    _cache_service: Any
    _graph_sync: Any

    async def ingest_with_supersession(
        self,
        *,
        org_id: UUID,
        project_id: UUID,
        user_id: UUID,
        facts: list[dict[str, Any]],
        source_episode_id: UUID | None = None,
        insert_mode: Literal["batch_create", "batch_create_or_skip"] = "batch_create",
        now: datetime | None = None,
    ) -> FactIngestionResult:
        """Persist a batch of facts, superseding conflicting active facts.

        Args:
            org_id: Tenant scope.
            project_id: Project scope.
            user_id: Fact owner (attribution).
            facts: Fact dicts with keys ``subject``, ``predicate``,
                ``object`` and optional ``content``, ``confidence``,
                ``subject_type``, ``object_type``,
                ``subject_entity_id``, ``object_entity_id``.
            source_episode_id: Source episode for the extraction worker
                path; ``None`` for API/business-data ingestion.
            insert_mode: Which repository insert primitive to use —
                ``batch_create`` (API) or ``batch_create_or_skip``
                (extraction worker, keeps the episode-scoped exclusion
                constraint as a safety net).
            now: Effective-at instant for the supersession; defaults to
                the current UTC time.

        Returns:
            A :class:`FactIngestionResult` with the created rows and
            inserted/superseded/skipped counts.

        Raises:
            Exception: Propagates DB errors; the caller's transaction is
                the single commit/rollback point, so a raise here undoes
                the whole batch including supersessions.
        """
        from functools import partial

        now = now or datetime.now(UTC)
        if not facts:
            return FactIngestionResult()

        entries = [self._prepare_entry(f, source_episode_id, now) for f in facts]

        # In-batch dedup of identical (NAME identity, content) — the
        # identical-content skip applies within a batch too, and across
        # identity forms: entity + literal duplicates of one assertion
        # collapse to the first occurrence (the first inserts, later ones
        # skip without a DB round trip).
        seen: set[tuple[Any, str]] = set()
        deduped: list[dict[str, Any]] = []
        for entry in entries:
            key = (entry["name_identity"], entry["row"]["content"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(entry)
        skipped_count = len(entries) - len(deduped)
        entries = deduped

        # ── 0. Serialize concurrent writers per conflict identity ─────────
        # FOR UPDATE cannot lock a row that does not exist yet, so two
        # writers whose scans both come up empty would both insert —
        # silent coexistence.  An advisory xact lock keyed on the NAME
        # identity closes that gap: the loser blocks here, and its re-scan
        # (after the winner commits) sees the winner's row and supersedes.
        # Sorted acquisition keeps multi-identity batches deadlock-free.
        lock_keys = sorted(
            {
                self._lock_key_for_identity(org_id, project_id, entry["name_identity"])
                for entry in entries
            }
        )
        if lock_keys:
            await self._fact_repo.lock_conflict_identities(lock_keys)

        # ── 1. Conflict scan (one query, rows locked until caller commits) ──
        # Entity-form entries emit BOTH their UUID key and their SPO-string
        # key so the scan also returns literal (unresolved) candidates for
        # the cross-form match — a resolved-only key would miss them.
        candidates = await self._fact_repo.find_conflicting_active_for_update(
            org_id=org_id,
            project_id=project_id,
            match_keys=[k for e in entries for k in e["match_keys"]],
            now=now,
        )
        # Candidates are bucketed by their NAME identity (not the
        # form-sensitive one) so a string entry finds entity candidates and
        # vice versa; ``_candidate_conflicts`` applies the precise rule.
        candidates_by_identity: dict[Any, list[Fact]] = defaultdict(list)
        for candidate in candidates:
            candidates_by_identity[
                self._name_identity_of_fact(candidate)
            ].append(candidate)

        # ── 1b. Tombstone scan (ADR 005: retracted --> [*], terminal) ────
        tombstones_by_identity = await self._scan_tombstones(
            org_id, project_id, entries
        )

        # Identities occurring once are batch-safe (one INSERT statement);
        # repeated NAME identities need sequential handling so the second
        # occurrence supersedes the first inside the batch.
        identity_counts = Counter(e["name_identity"] for e in entries)
        batch_entries = [
            e for e in entries if identity_counts[e["name_identity"]] == 1
        ]
        sequential_entries = [
            e for e in entries if identity_counts[e["name_identity"]] > 1
        ]

        ctx = _BatchCtx(
            org_id=org_id,
            project_id=project_id,
            user_id=user_id,
            source_episode_id=source_episode_id,
            insert_mode=insert_mode,
            now=now,
        )
        state = _BatchState()

        # ── 2a/2b. Supersede + insert (batch-safe, then sequential) ──────
        await self._supersede_batch_safe(
            batch_entries, candidates_by_identity, tombstones_by_identity, ctx, state
        )
        await self._supersede_sequential(
            sequential_entries,
            candidates_by_identity,
            tombstones_by_identity,
            ctx,
            state,
        )

        # ── 2c. Lineage: superseded_by_fact_id + invalidation row ──────────
        await self._record_lineage(org_id, project_id, now, state)

        # ── 3. Post-commit side effects (deferred to the real commit) ──────
        # The caller owns the commit point, so firing here would emit
        # phantom FACT_SUPERSEDED events, purge caches, and bump metrics
        # for transactions the caller later rolls back.  Queue them and
        # let the session's after_commit hook fire them once the
        # transaction actually commits; after_rollback drops them.
        if state.superseded_count:
            self._queue_post_commit_effect(
                partial(self._bump_superseded_total, state.superseded_count)
            )
            if self._cache_service is not None:
                self._queue_post_commit_effect(
                    partial(
                        self._cache_service.invalidate_project_context,
                        str(org_id),
                        str(project_id),
                    )
                )
        if state.events and self._webhook_service is not None:
            for event in state.events:
                if event.new_fact_id is None:
                    continue  # retraction — no successor, nothing to webhook
                self._queue_post_commit_effect(
                    self._make_webhook_effect(
                        org_id=org_id,
                        project_id=project_id,
                        old_id=event.old_fact_id,
                        new_id=event.new_fact_id,
                        triple=event.triple,
                    )
                )
        # Edge expiry synchronisation — queued alongside the webhook/cache
        # effects so it fires only after the caller's commit (a rolled-back
        # transaction emits no edge expiries either).
        if state.events and self._graph_sync is not None:
            self._queue_post_commit_effect(
                self._make_graph_sync_effect(
                    org_id=org_id,
                    project_id=project_id,
                    events=state.events,
                    at_time=now,
                )
            )

        logger.info(
            "fact_invalidation.ingested",
            extra={
                "org_id": str(org_id),
                "project_id": str(project_id),
                "input_count": len(facts),
                "inserted_count": len(state.created),
                "superseded_count": state.superseded_count,
                "skipped_count": skipped_count + state.skipped_count,
            },
        )
        return FactIngestionResult(
            created=state.created,
            inserted_count=len(state.created),
            superseded_count=state.superseded_count,
            skipped_count=skipped_count + state.skipped_count,
        )

    @staticmethod
    async def _bump_superseded_total(count: int) -> None:
        """Post-commit metric bump (async wrapper so it queues like the rest).

        Args:
            count: Number of superseded facts to add to the counter.
        """
        from services import (  # via facade (patchable)
            fact_invalidation_service as _facade,
        )

        _facade.facts_superseded_total.inc(count)
