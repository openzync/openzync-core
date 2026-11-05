"""Fact persistence — inserts, tombstone scans, and supersession writes."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from models.fact import Fact
from services.facts.conflict_matching import NameIdentity

logger = logging.getLogger(__name__)


@dataclass
class _BatchCtx:
    """Immutable per-batch scope for the supersession writes."""

    org_id: UUID
    project_id: UUID
    user_id: UUID
    source_episode_id: UUID | None
    insert_mode: Literal["batch_create", "batch_create_or_skip"]
    now: datetime


@dataclass
class _BatchState:
    """Mutable accumulators threaded through the supersession writes."""

    created: list[Fact] = field(default_factory=list)
    closed_ids: set[UUID] = field(default_factory=set)
    in_batch: dict[NameIdentity, list[Fact]] = field(
        default_factory=lambda: defaultdict(list)
    )
    superseded_count: int = 0
    skipped_count: int = 0
    events: list[Any] = field(default_factory=list)
    source_episode_by_old_fact: dict[UUID, UUID | None] = field(
        default_factory=dict
    )


class FactPersistenceMixin:
    """Batch-safe and sequential supersession writes plus lineage."""

    _db: Any
    _fact_repo: Any

    async def _scan_tombstones(
        self,
        org_id: UUID,
        project_id: UUID,
        entries: list[dict[str, Any]],
    ) -> dict[NameIdentity, list[Fact]]:
        """Scan retracted rows colliding with the batch (one query).

        Superseded/expired-only rows (invalid_at NULL) never match —
        re-assertion after supersession still inserts.

        Args:
            org_id: Tenant scope.
            project_id: Project scope.
            entries: Prepared batch entries (match keys scanned).

        Returns:
            Tombstones bucketed by NAME identity for the cross-form match.
        """
        tombstones_by_identity: dict[NameIdentity, list[Fact]] = defaultdict(list)
        tombstone_keys = sorted(
            {k for e in entries for k in e["match_keys"]}, key=repr
        )
        if not tombstone_keys:
            return tombstones_by_identity
        for tombstone in await self._fact_repo.find_retracted_by_match_keys(
            org_id=org_id,
            project_id=project_id,
            match_keys=tombstone_keys,
        ):
            tombstones_by_identity[self._name_identity_of_fact(tombstone)].append(
                tombstone
            )
        return tombstones_by_identity

    async def _supersede_batch_safe(
        self,
        batch_entries: list[dict[str, Any]],
        candidates_by_identity: dict[NameIdentity, list[Fact]],
        tombstones_by_identity: dict[NameIdentity, list[Fact]],
        ctx: _BatchCtx,
        state: _BatchState,
    ) -> None:
        """Supersede single-occurrence identities, then one bulk insert.

        Skips identical-content conflicts (idempotent retry) and
        tombstoned identities (retraction wins — no insert, no event).

        Args:
            batch_entries: Entries whose NAME identity occurs once.
            candidates_by_identity: Active candidates bucketed by identity.
            tombstones_by_identity: Retracted rows bucketed by identity.
            ctx: Batch scope.
            state: Mutable accumulators (updated in place).
        """
        from services.graph_edge_sync_service import make_supersession_event

        batch_rows: list[dict] = []
        batch_kept: list[tuple[dict, list[Fact]]] = []
        for entry in batch_entries:
            conflicts = self._matching_conflicts(
                entry,
                candidates_by_identity.get(entry["name_identity"], ()),
                state.closed_ids,
            )
            if any(c.content == entry["row"]["content"] for c in conflicts):
                state.skipped_count += 1
                continue  # identical content — idempotent skip
            if any(
                self._candidate_conflicts(entry, t)
                for t in tombstones_by_identity.get(entry["name_identity"], ())
            ):
                state.skipped_count += 1
                logger.info(
                    "fact_invalidation.tombstone_skip",
                    extra={
                        "org_id": str(ctx.org_id),
                        "project_id": str(ctx.project_id),
                        "subject": entry["row"]["subject"],
                        "predicate": entry["row"]["predicate"],
                        "object": entry["row"]["object"],
                    },
                )
                continue  # retracted tombstone wins — no insert, no event
            for c in conflicts:
                await self._fact_repo.set_valid_to(c.id, ctx.now)
                state.closed_ids.add(c.id)
                state.superseded_count += 1
            batch_rows.append(entry["row"])
            batch_kept.append((entry, conflicts))

        if batch_rows:
            state.created.extend(
                await self._insert_rows(
                    ctx.org_id,
                    ctx.project_id,
                    ctx.user_id,
                    ctx.source_episode_id,
                    batch_rows,
                    ctx.insert_mode,
                )
            )
        # Pairing is NAME-based so cross-form supersessions still emit the
        # supersession event — a literal successor of an entity fact must
        # expire the old edge (D1 case 2), and vice versa.
        created_by_identity: dict[NameIdentity, list[Fact]] = defaultdict(list)
        for fact in state.created:
            created_by_identity[self._name_identity_of_fact(fact)].append(fact)
        for entry, old_facts in batch_kept:
            new_rows = created_by_identity.get(entry["name_identity"], [])
            if not new_rows:  # row skipped by ON CONFLICT — nothing to pair
                continue
            for old_fact in old_facts:
                state.source_episode_by_old_fact[old_fact.id] = entry["row"].get(
                    "source_episode_id"
                )
                state.events.append(
                    make_supersession_event(old_fact, new_rows[0], entry["triple"])
                )

    async def _supersede_sequential(
        self,
        sequential_entries: list[dict[str, Any]],
        candidates_by_identity: dict[NameIdentity, list[Fact]],
        tombstones_by_identity: dict[NameIdentity, list[Fact]],
        ctx: _BatchCtx,
        state: _BatchState,
    ) -> None:
        """Supersede repeated identities one at a time, in batch order.

        The second occurrence of an identity supersedes the first inside
        the batch (visible via ``state.in_batch``).

        Args:
            sequential_entries: Entries whose NAME identity repeats.
            candidates_by_identity: Active candidates bucketed by identity.
            tombstones_by_identity: Retracted rows bucketed by identity.
            ctx: Batch scope.
            state: Mutable accumulators (updated in place).
        """
        from services.graph_edge_sync_service import make_supersession_event

        for entry in sequential_entries:
            name_identity = entry["name_identity"]
            conflicts = self._matching_conflicts(
                entry,
                list(candidates_by_identity.get(name_identity, ()))
                + list(state.in_batch.get(name_identity, ())),
                state.closed_ids,
            )
            if any(c.content == entry["row"]["content"] for c in conflicts):
                state.skipped_count += 1
                continue
            if any(
                self._candidate_conflicts(entry, t)
                for t in tombstones_by_identity.get(name_identity, ())
            ):
                state.skipped_count += 1
                logger.info(
                    "fact_invalidation.tombstone_skip",
                    extra={
                        "org_id": str(ctx.org_id),
                        "project_id": str(ctx.project_id),
                        "subject": entry["row"]["subject"],
                        "predicate": entry["row"]["predicate"],
                        "object": entry["row"]["object"],
                    },
                )
                continue  # retracted tombstone wins — no insert, no event
            old_facts: list[Fact] = []
            for c in conflicts:
                await self._fact_repo.set_valid_to(c.id, ctx.now)
                old_facts.append(c)
                state.superseded_count += 1
                if c in state.in_batch.get(name_identity, ()):
                    state.in_batch[name_identity].remove(c)
                else:
                    state.closed_ids.add(c.id)
            inserted = await self._insert_rows(
                ctx.org_id,
                ctx.project_id,
                ctx.user_id,
                ctx.source_episode_id,
                [entry["row"]],
                ctx.insert_mode,
            )
            if inserted:
                state.in_batch[name_identity].extend(inserted)
                state.created.extend(inserted)
                for old_fact in old_facts:
                    state.source_episode_by_old_fact[old_fact.id] = entry["row"].get(
                        "source_episode_id"
                    )
                    state.events.append(
                        make_supersession_event(
                            old_fact, inserted[0], entry["triple"]
                        )
                    )

    async def _record_lineage(
        self,
        org_id: UUID,
        project_id: UUID,
        now: datetime,
        state: _BatchState,
    ) -> None:
        """Persist supersession lineage + invalidation rows (same txn).

        One row per real old-fact → successor transition. Events are
        unique per (old, new) pair and the identical-content skip already
        prevents repeats, so no dedup is needed here.

        Args:
            org_id: Tenant scope.
            project_id: Project scope.
            now: Supersession instant.
            state: Mutable accumulators (read-only here).
        """
        for transition in state.events:
            if transition.new_fact_id is None:
                continue  # retraction-style — none in the deterministic path
            await self._fact_repo.set_superseded_by(
                transition.old_fact_id, transition.new_fact_id
            )
            await self._fact_repo.record_invalidation_event(
                organization_id=org_id,
                project_id=project_id,
                old_fact_id=transition.old_fact_id,
                new_fact_id=transition.new_fact_id,
                kind="superseded",
                reason=None,  # deterministic path carries no reason
                at_time=now,
                source_episode_id=state.source_episode_by_old_fact.get(
                    transition.old_fact_id
                ),
            )

    def _matching_conflicts(
        self,
        entry: dict[str, Any],
        scope: list[Fact] | tuple[Fact, ...],
        closed_ids: set[UUID] | frozenset[UUID] = frozenset(),
    ) -> list[Fact]:
        """Candidates in ``scope`` that the entry actually supersedes.

        Args:
            entry: A prepared batch entry.
            scope: Candidate facts to test.
            closed_ids: Facts already closed in this batch (skipped).

        Returns:
            The conflicting candidates.
        """
        return [
            c
            for c in scope
            if c.id not in closed_ids and self._candidate_conflicts(entry, c)
        ]

    async def _insert_rows(
        self,
        org_id: UUID,
        project_id: UUID,
        user_id: UUID,
        source_episode_id: UUID | None,
        rows: list[dict[str, Any]],
        insert_mode: Literal["batch_create", "batch_create_or_skip"],
    ) -> list[Fact]:
        """Insert rows via the configured repository primitive."""
        if insert_mode == "batch_create_or_skip":
            return await self._fact_repo.batch_create_or_skip(
                organization_id=org_id,
                project_id=project_id,
                user_id=user_id,
                source_episode_id=source_episode_id,  # type: ignore[arg-type]
                facts=rows,
            )
        return await self._fact_repo.batch_create(
            organization_id=org_id,
            project_id=project_id,
            user_id=user_id,
            facts=rows,
        )
