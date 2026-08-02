"""Fact invalidation service — automatic supersession of conflicting facts.

Replaces the three-way-inconsistent conflict behaviour (extraction worker
silently drops, API rejects with 409, cross-episode conflicts coexist)
with a single supersession state machine:

1. Scan for active facts sharing the incoming fact's conflict identity
   (``SELECT ... FOR UPDATE``, one query per batch).
2. If a conflicting active fact has **identical content**, skip the insert
   entirely — makes ARQ retries idempotent.
3. Otherwise close the old fact's range (``set_valid_to(now)``) and insert
   the new fact with ``valid_from = now`` in the **same transaction**.

The service never commits — the caller's transaction (worker session or
request-scoped session) is the single commit point, which is what makes
the truncate + insert atomic.  Post-insert side effects (superseded
webhook, context-cache purge, metric) are queued and fire on the
session's ``after_commit`` event — never before the caller's commit, so
a rolled-back transaction emits no phantom events.  Embed enqueueing
stays with the callers, who already own per-path ``job_id`` /
``trace_id`` semantics.

Concurrency: writers take a PostgreSQL advisory xact lock per distinct
conflict identity before the conflict scan (``lock_conflict_identities``
in the repository).  ``FOR UPDATE`` cannot lock a row that does not
exist yet; the advisory lock serializes the scan+insert so two
concurrent writers for the same identity never coexist silently.

Conflict identity: ``(subject_entity_id, predicate, object_entity_id)``
when both entity IDs resolve, else normalized ``(subject, predicate,
object)`` strings.  Normalization reuses the aggressive canonicalization
from ``workers/tasks/extract_facts.py::_match_entity`` (lowercase, strip
punctuation).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal
from uuid import UUID

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from core.events import EventType
from middleware.metrics import facts_superseded_total
from models.fact import Fact
from repositories.fact_repository import FactRepository

if TYPE_CHECKING:
    from services.cache_service import CacheService
    from services.webhook_service import WebhookService

logger = logging.getLogger(__name__)

# ── Conflict identity normalization ───────────────────────────────────────────
# Must stay byte-identical to the aggressive normalization in
# ``workers/tasks/extract_facts.py::_match_entity`` (step 5) so the
# extraction pipeline and every write path agree on what "same triple" means.
_ENTITY_NAME_RE = re.compile(r"[^a-z0-9\s]")

# ── Context-cache purge ───────────────────────────────────────────────────────
# CacheService requires a non-None ``default_ttl`` at construction, but the
# purge path (``invalidate_project_context``) is scan+delete only and never
# reads it — the context writer passes an explicit TTL (``context_service``
# hardcodes 30s) and the org's real ``context_cache_ttl`` only gates whether a
# cache exists at all.  This placeholder satisfies the constructor contract
# for the purge path; it is never used for cache expiry.
PURGE_ONLY_CACHE_TTL: int = 30


def normalize_identity_term(term: str | None) -> str:
    """Canonicalize a triple term for conflict-identity comparison.

    Args:
        term: Raw subject/predicate/object string, possibly ``None``.

    Returns:
        Lowercased term with punctuation stripped and surrounding
        whitespace removed.
    """
    if not term:
        return ""
    return _ENTITY_NAME_RE.sub("", term.lower()).strip()


IdentityKey = tuple[UUID | str, str, UUID | str]


async def _inc_superseded_total(count: int) -> None:
    """Post-commit metric bump (async wrapper so it queues like the rest).

    Args:
        count: Number of superseded facts to add to the counter.
    """
    facts_superseded_total.inc(count)


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


class FactInvalidationService:
    """Orchestrates the supersession state machine for a fact batch.

    Args:
        db: An async SQLAlchemy session (transaction owned by the caller).
        fact_repo: Repository providing the conflict scan and
            ``set_valid_to`` primitives.
        webhook_service: Optional — emits ``FACT_SUPERSEDED`` webhooks
            when provided (API path).
        cache_service: Optional — purges the project context-cache
            prefix when provided (API path).
    """

    def __init__(
        self,
        db: AsyncSession,
        fact_repo: FactRepository,
        *,
        webhook_service: WebhookService | None = None,
        cache_service: CacheService | None = None,
    ) -> None:
        self._db = db
        self._fact_repo = fact_repo
        self._webhook_service = webhook_service
        self._cache_service = cache_service
        self._pending_effects: list[Callable[[], Awaitable[None]]] = []
        self._events_attached = False

    @staticmethod
    def _lock_key_for_identity(
        org_id: UUID, project_id: UUID, identity: IdentityKey
    ) -> str:
        """Derive the advisory-lock key for one conflict identity.

        Scoped by org + project so unrelated tenants never block each
        other on the same triple text.  Identity components are stable by
        construction (entity UUIDs, or already-normalized strings), so
        every writer computes the identical key for the same triple.

        Args:
            org_id: Tenant scope.
            project_id: Project scope.
            identity: A conflict identity ``(subject, predicate, object)``.

        Returns:
            A stable lock key for ``pg_advisory_xact_lock(hashtext(...))``.
        """
        subject, predicate, obj = identity
        return f"sup:{org_id}:{project_id}:{subject}:{predicate}:{obj}"

    # ── Public API ──────────────────────────────────────────────────────────────

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
        now = now or datetime.now(timezone.utc)
        if not facts:
            return FactIngestionResult()

        skipped_count = 0
        entries = [self._prepare_entry(f, source_episode_id, now) for f in facts]

        # In-batch dedup of identical (identity, content) — the identical-
        # content skip applies within a batch too, not just against existing
        # rows: the first occurrence inserts, later ones are skipped without
        # a DB round trip.
        seen: set[tuple[IdentityKey, str]] = set()
        deduped: list[dict[str, Any]] = []
        for entry in entries:
            key = (entry["identity"], entry["row"]["content"])
            if key in seen:
                skipped_count += 1
                continue
            seen.add(key)
            deduped.append(entry)
        entries = deduped

        # ── 0. Serialize concurrent writers per conflict identity ─────────
        # FOR UPDATE cannot lock a row that does not exist yet, so two
        # writers whose scans both come up empty would both insert —
        # silent coexistence.  An advisory xact lock keyed on the
        # identity closes that gap: the loser blocks here, and its re-scan
        # (after the winner commits) sees the winner's row and supersedes.
        # Sorted acquisition keeps multi-identity batches deadlock-free.
        lock_keys = sorted(
            {
                self._lock_key_for_identity(org_id, project_id, entry["identity"])
                for entry in entries
            }
        )
        if lock_keys:
            await self._fact_repo.lock_conflict_identities(lock_keys)

        # ── 1. Conflict scan (one query, rows locked until caller commits) ──
        candidates = await self._fact_repo.find_conflicting_active_for_update(
            org_id=org_id,
            project_id=project_id,
            match_keys=[e["match_key"] for e in entries],
            now=now,
        )
        candidates_by_identity: dict[IdentityKey, list[Fact]] = defaultdict(list)
        for candidate in candidates:
            candidates_by_identity[self._identity_of_fact(candidate)].append(candidate)

        # Identities occurring once are batch-safe (one INSERT statement);
        # repeated identities need sequential handling so the second
        # occurrence supersedes the first inside the batch.
        identity_counts = Counter(e["identity"] for e in entries)
        batch_entries = [e for e in entries if identity_counts[e["identity"]] == 1]
        sequential_entries = [e for e in entries if identity_counts[e["identity"]] > 1]

        created: list[Fact] = []
        superseded_count = 0
        closed_ids: set[UUID] = set()
        in_batch: dict[IdentityKey, list[Fact]] = defaultdict(list)
        # (old_fact_id, new_fact_id, triple) — resolved at pairing time so
        # repeated identities in one batch pair each old fact with the
        # exact new fact that replaced it.
        supersession_events: list[tuple[UUID, UUID, dict]] = []

        # ── 2a. Batch-safe identities: supersede, then one bulk insert ────
        batch_rows: list[dict] = []
        batch_entries_kept: list[tuple[dict, list[UUID]]] = []
        for entry in batch_entries:
            conflicts = [
                c
                for c in candidates_by_identity.get(entry["identity"], ())
                if c.id not in closed_ids
            ]
            if any(c.content == entry["row"]["content"] for c in conflicts):
                skipped_count += 1
                continue  # identical content — idempotent skip, no truncation
            for c in conflicts:
                await self._fact_repo.set_valid_to(c.id, now)
                closed_ids.add(c.id)
                superseded_count += 1
            batch_rows.append(entry["row"])
            batch_entries_kept.append((entry, [c.id for c in conflicts]))

        if batch_rows:
            created.extend(await self._insert_rows(
                org_id, project_id, user_id, source_episode_id, batch_rows, insert_mode
            ))
        created_by_identity: dict[IdentityKey, list[Fact]] = defaultdict(list)
        for fact in created:
            created_by_identity[self._identity_of_fact(fact)].append(fact)
        for entry, old_ids in batch_entries_kept:
            new_rows = created_by_identity.get(entry["identity"], [])
            if not new_rows:  # row skipped by ON CONFLICT — nothing to pair
                continue
            for old_id in old_ids:
                supersession_events.append((old_id, new_rows[0].id, entry["triple"]))

        # ── 2b. Repeated identities: sequential supersede + insert ────────
        for entry in sequential_entries:
            identity = entry["identity"]
            conflicts = (
                [
                    c
                    for c in candidates_by_identity.get(identity, ())
                    if c.id not in closed_ids
                ]
                + list(in_batch.get(identity, ()))
            )
            if any(c.content == entry["row"]["content"] for c in conflicts):
                skipped_count += 1
                continue
            old_ids: list[UUID] = []
            for c in conflicts:
                await self._fact_repo.set_valid_to(c.id, now)
                old_ids.append(c.id)
                superseded_count += 1
                if c in in_batch.get(identity, ()):
                    in_batch[identity].remove(c)
                else:
                    closed_ids.add(c.id)
            inserted = await self._insert_rows(
                org_id,
                project_id,
                user_id,
                source_episode_id,
                [entry["row"]],
                insert_mode,
            )
            if inserted:
                in_batch[identity].extend(inserted)
                created.extend(inserted)
                for old_id in old_ids:
                    supersession_events.append(
                        (old_id, inserted[0].id, entry["triple"])
                    )

        # ── 3. Post-commit side effects (deferred to the real commit) ──────
        # The caller owns the commit point, so firing here would emit
        # phantom FACT_SUPERSEDED events, purge caches, and bump metrics
        # for transactions the caller later rolls back.  Queue them and
        # let the session's after_commit hook fire them once the
        # transaction actually commits; after_rollback drops them.
        if superseded_count:
            self._queue_post_commit_effect(
                partial(_inc_superseded_total, superseded_count)
            )
            if self._cache_service is not None:
                self._queue_post_commit_effect(
                    partial(
                        self._cache_service.invalidate_project_context,
                        str(org_id),
                        str(project_id),
                    )
                )
        if supersession_events and self._webhook_service is not None:
            for old_id, new_id, triple in supersession_events:
                self._queue_post_commit_effect(
                    self._make_webhook_effect(
                        org_id=org_id,
                        project_id=project_id,
                        old_id=old_id,
                        new_id=new_id,
                        triple=triple,
                    )
                )

        logger.info(
            "fact_invalidation.ingested",
            extra={
                "org_id": str(org_id),
                "project_id": str(project_id),
                "input_count": len(facts),
                "inserted_count": len(created),
                "superseded_count": superseded_count,
                "skipped_count": skipped_count,
            },
        )
        return FactIngestionResult(
            created=created,
            inserted_count=len(created),
            superseded_count=superseded_count,
            skipped_count=skipped_count,
        )

    # ── Internal helpers ────────────────────────────────────────────────────────

    def _prepare_entry(
        self,
        fact: dict[str, Any],
        source_episode_id: UUID | None,
        now: datetime,
    ) -> dict[str, Any]:
        """Normalize one input fact into a row + identity + match key."""
        subject = str(fact.get("subject") or "").strip()
        predicate = str(fact.get("predicate") or "").strip()
        obj = str(fact.get("object") or "").strip()
        subject_entity_id = fact.get("subject_entity_id")
        object_entity_id = fact.get("object_entity_id")
        if isinstance(subject_entity_id, str):
            subject_entity_id = UUID(subject_entity_id) if subject_entity_id else None
        if isinstance(object_entity_id, str):
            object_entity_id = UUID(object_entity_id) if object_entity_id else None

        identity: IdentityKey
        match_key: tuple[UUID | str, str, UUID | str]
        if subject_entity_id is not None and object_entity_id is not None:
            identity = (subject_entity_id, predicate, object_entity_id)
            match_key = (subject_entity_id, predicate, object_entity_id)
        else:
            identity = (
                normalize_identity_term(subject),
                normalize_identity_term(predicate),
                normalize_identity_term(obj),
            )
            match_key = (subject, predicate, obj)

        return {
            "identity": identity,
            "match_key": match_key,
            "triple": {"subject": subject, "predicate": predicate, "object": obj},
            "row": {
                "subject": subject,
                "predicate": predicate,
                "object": obj,
                "content": fact.get("content") or f"{subject} {predicate} {obj}",
                "confidence": float(fact.get("confidence", 1.0)),
                "subject_type": fact.get("subject_type", "literal"),
                "object_type": fact.get("object_type", "literal"),
                "subject_entity_id": subject_entity_id,
                "object_entity_id": object_entity_id,
                "source_episode_id": source_episode_id,
                "valid_from": now,  # explicit — superseding fact starts now
                "valid_to": None,
            },
        }

    @staticmethod
    def _identity_of_fact(fact: Fact) -> IdentityKey:
        """Compute the conflict identity of a stored fact row."""
        if fact.subject_entity_id is not None and fact.object_entity_id is not None:
            return (fact.subject_entity_id, fact.predicate, fact.object_entity_id)
        return (
            normalize_identity_term(fact.subject),
            normalize_identity_term(fact.predicate),
            normalize_identity_term(fact.object),
        )

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

    # ── Post-commit side effects ─────────────────────────────────────────────

    def _queue_post_commit_effect(self, effect: Callable[[], Awaitable[None]]) -> None:
        """Queue an async effect to run only after the caller commits.

        Attaches the session's one-shot commit hooks on first use; the
        hooks drain the queue and stay attached (idempotent no-ops) for
        the session's lifetime, so a reused session never double-fires.

        Args:
            effect: Zero-argument async callable to run post-commit.
        """
        self._pending_effects.append(effect)
        self._attach_post_commit_events()

    def _make_webhook_effect(
        self,
        *,
        org_id: UUID,
        project_id: UUID,
        old_id: UUID,
        new_id: UUID,
        triple: dict[str, str],
    ) -> Callable[[], Awaitable[None]]:
        """Build the post-commit ``FACT_SUPERSEDED`` webhook effect.

        The endpoint lookup inside ``WebhookService.emit`` runs on the
        injected service's session.  By the time this effect fires, the
        request session is mid-teardown (``close()`` races the checkout),
        so a real service bound to that session is rebuilt against a
        fresh session for the lookup.  Mocks (unit tests) pass through
        untouched.

        Args:
            org_id: Tenant scope (webhook recipients).
            project_id: Project scope (payload only).
            old_id: Superseded fact ID (payload only).
            new_id: Replacing fact ID (payload only).
            triple: The SPO triple (payload only).

        Returns:
            An async effect that emits the webhook once the txn commits.
        """
        from repositories.webhook_repository import WebhookRepository
        from services.webhook_service import WebhookService

        payload = {
            "old_fact_id": str(old_id),
            "new_fact_id": str(new_id),
            "triple": triple,
            "project_id": str(project_id),
            "org_id": str(org_id),
        }

        def _reuse() -> bool:
            return (
                isinstance(self._webhook_service, WebhookService)
                and getattr(self._webhook_service, "_repo", None) is not None
                and getattr(self._webhook_service._repo, "_db", None) is self._db
            )

        async def _effect() -> None:
            fresh: AsyncSession | None = None
            service = self._webhook_service
            if _reuse():
                fresh = AsyncSession(bind=self._db.bind, expire_on_commit=False)
                service = WebhookService(repo=WebhookRepository(fresh))
            try:
                await service.emit(
                    organization_id=org_id,
                    event_type=EventType.FACT_SUPERSEDED,
                    payload=payload,
                )
            finally:
                if fresh is not None:
                    await fresh.close()

        return _effect

    def _attach_post_commit_events(self) -> None:
        """Attach ``after_commit``/``after_rollback`` hooks to the session.

        Idempotent per service instance.  Listeners stay attached for the
        session's lifetime (per-request / per-task here) and no-op when
        the queue is empty — they must never be removed from inside the
        handler itself, since the event dispatcher iterates the listener
        list while dispatching.
        """
        if self._events_attached:
            return
        event.listen(self._db.sync_session, "after_commit", self._on_after_commit)
        event.listen(self._db.sync_session, "after_rollback", self._on_after_rollback)
        self._events_attached = True

    def _on_after_commit(self, _session: Session) -> None:
        """Sync event hook — schedule queued effects once the txn commits.

        Runs inside ``await db.commit()`` where the event loop is active,
        so the queued async work is dispatched as a task rather than
        awaited here.  Failures inside that task are logged loudly — the
        transaction is already committed, so there is nothing left to
        roll back.  No-op when nothing is queued.
        """
        pending, self._pending_effects = self._pending_effects, []
        if not pending:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.error(
                "fact_invalidation.post_commit_no_loop",
                extra={"queued_effects": len(pending)},
            )
            return
        loop.create_task(self._run_pending_effects(pending))

    def _on_after_rollback(self, _session: Session) -> None:
        """Sync event hook — drop queued effects, the txn was rolled back."""
        self._pending_effects = []

    async def _run_pending_effects(
        self, pending: list[Callable[[], Awaitable[None]]]
    ) -> None:
        """Await queued post-commit effects, logging any failure loudly."""
        for effect in pending:
            try:
                await effect()
            except Exception:
                logger.exception("fact_invalidation.post_commit_effect_failed")
