"""Fact post-commit effects — queued side effects fired after commit."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from core.events import EventType
from middleware.metrics import (
    facts_invalidated_total,
    facts_retracted_total,
    facts_superseded_total,
)

if TYPE_CHECKING:
    from packages.graph_backend.interface import GraphBackend
    from services.cache_service import CacheService
    from services.graph_edge_sync_service import SupersessionEvent

logger = logging.getLogger(__name__)

# CacheService requires a non-None ``default_ttl`` at construction, but the
# purge path (``invalidate_project_context``) is scan+delete only and never
# reads it — the context writer passes an explicit TTL (``context_service``
# hardcodes 30s) and the org's real ``context_cache_ttl`` only gates whether a
# cache exists at all.  This placeholder satisfies the constructor contract
# for the purge path; it is never used for cache expiry.
PURGE_ONLY_CACHE_TTL: int = 30
async def _inc_superseded_total(count: int) -> None:
    """Post-commit metric bump (async wrapper so it queues like the rest).

    Args:
        count: Number of superseded facts to add to the counter.
    """
    facts_superseded_total.inc(count)


async def _inc_retracted_total(count: int) -> None:
    """Post-commit metric bump for manual retractions.

    Args:
        count: Number of retracted facts to add to the counter.
    """
    facts_retracted_total.inc(count)


async def _inc_invalidated_total(count: int) -> None:
    """Post-commit metric bump for LLM-driven invalidations.

    Args:
        count: Number of invalidated facts to add to the counter.
    """
    facts_invalidated_total.inc(count)


async def _purge_project_context(
    cache_service: CacheService, org_id: UUID, project_id: UUID
) -> None:
    """Post-commit context-cache purge wrapper.

    Discards ``invalidate_project_context``'s deleted-key count so the
    effect matches the queue's ``Awaitable[None]`` contract.

    Args:
        cache_service: The cache service to purge through.
        org_id: Tenant scope (cache key prefix).
        project_id: Project scope (cache key prefix).
    """
    await cache_service.invalidate_project_context(str(org_id), str(project_id))
    # Bump the generation too: versioned ctx:..:v{gen} keys minted after the
    # SCAN+DEL must not reuse the pre-purge generation.
    await cache_service.bump_context_version(str(org_id), str(project_id))

class PostCommitEffectsMixin:
    """Composed into :class:`FactInvalidationService`."""

    _db: Any
    _fact_repo: Any
    _webhook_service: Any
    _cache_service: Any
    _graph_sync: Any
    _pending_effects: list
    _events_attached: bool

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
        new_id: UUID | None,
        triple: dict[str, str],
        event_type: str = EventType.FACT_SUPERSEDED,
        reason: str | None = None,
    ) -> Callable[[], Awaitable[None]]:
        """Build a post-commit fact webhook effect.

        The endpoint lookup inside ``WebhookService.emit`` runs on the
        injected service's session.  By the time this effect fires, the
        request session is mid-teardown (``close()`` races the checkout),
        so a real service bound to that session is rebuilt against a
        fresh session for the lookup.  Mocks (unit tests) pass through
        untouched.

        Args:
            org_id: Tenant scope (webhook recipients).
            project_id: Project scope (payload only).
            old_id: Superseded/invalidated fact ID (payload only).
            new_id: Replacing fact ID, or ``None`` for a retraction-style
                invalidation (payload only).
            triple: The SPO triple (payload only).
            event_type: The event to emit — defaults to
                ``FACT_SUPERSEDED``; retraction and LLM invalidation
                paths pass their own.
            reason: Optional invalidation reason, included in the payload
                only when set (keeps the superseded payload shape stable).

        Returns:
            An async effect that emits the webhook once the txn commits.
        """
        from repositories.webhook_repository import WebhookRepository
        from services.webhook_service import WebhookService

        payload = {
            "old_fact_id": str(old_id),
            "new_fact_id": str(new_id) if new_id is not None else None,
            "triple": triple,
            "project_id": str(project_id),
            "org_id": str(org_id),
        }
        if reason is not None:
            payload["reason"] = reason

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
                from core import rls

                fresh = AsyncSession(bind=self._db.bind, expire_on_commit=False)
                # The fresh session checks out its own pooled connection,
                # which may carry no tenant (or a stale one).  The
                # ``webhook_endpoints`` lookup inside ``emit`` is
                # RLS-scoped, so bind the session to the emitting org
                # before it reads — an un-bound session matches no rows and
                # the event would be silently dropped.
                await rls.init_context(fresh, str(org_id))
                service = WebhookService(repo=WebhookRepository(fresh))
            try:
                await service.emit(
                    organization_id=org_id,
                    event_type=event_type,
                    payload=payload,
                )
            finally:
                if fresh is not None:
                    await fresh.close()

        return _effect
    def _make_graph_sync_effect(
        self,
        *,
        org_id: UUID,
        project_id: UUID,
        events: list[SupersessionEvent],
        at_time: datetime,
    ) -> Callable[[], Awaitable[None]]:
        """Build the post-commit graph edge-sync effect.

        The effect runs once the caller's transaction has committed —
        the after_commit queue already guarantees a rolled-back
        transaction emits nothing.  Postgres backends bound to the
        request session are rebuilt against a fresh session (by the time
        this effect fires the request session is mid-teardown — the same
        pattern as the webhook effect) and the expiry is committed in
        that fresh session.  Failures propagate to
        ``_run_pending_effects`` which logs them loudly; the fact commit
        is already durable and unaffected.

        Args:
            org_id: Tenant scope.
            project_id: Project scope.
            events: Supersession transitions recorded during ingest.
            at_time: The supersession instant (deterministic).
        """
        from core import rls
        from packages.graph_backend.postgres import PostgresGraphBackend
        from services.graph_edge_sync_service import GraphEdgeSyncService

        async def _effect() -> None:
            sync = self._graph_sync
            if sync is None:
                return
            fresh_sessions: list[AsyncSession] = []
            try:
                rebuilt: list[GraphBackend] = []
                for backend in sync.backends:
                    if (
                        isinstance(backend, PostgresGraphBackend)
                        and getattr(backend, "_db", None) is self._db
                    ):
                        fresh = AsyncSession(
                            bind=self._db.bind, expire_on_commit=False
                        )
                        fresh_sessions.append(fresh)
                        # The fresh session checks out its own pooled
                        # connection, which may carry a tenant (or a stale
                        # bypass) from an earlier request.  Initialise the
                        # context before the backend reads the graph
                        # tables — they are RLS-protected, so an
                        # uninitialised session would silently match no
                        # rows and the expiry would be a no-op.
                        await rls.init_context(fresh, str(org_id))
                        rebuilt.append(
                            PostgresGraphBackend(
                                db=fresh,
                                max_traversal_depth=backend._max_depth,
                            )
                        )
                    else:
                        rebuilt.append(backend)
                await GraphEdgeSyncService(
                    backends=rebuilt, arq_pool=sync.arq_pool
                ).sync_supersessions(
                    org_id=org_id,
                    project_id=project_id,
                    events=events,
                    at_time=at_time,
                )
                for fresh in fresh_sessions:
                    await fresh.commit()
            finally:
                for fresh in fresh_sessions:
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
