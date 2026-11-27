"""Cleanup worker — removes orphaned S3 blobs for soft-deleted episodes.

Runs as a low-priority scheduled ARQ task (daily 03:00 UTC cron) and can
also be triggered manually per-org.

Two modes:

* **Scheduled / org-discovery** (``user_id=None``): discovers all
  organization IDs via ``select(Organization.id)`` and processes each org
  independently, collecting per-org failures into a summary dict.
* **Manual / single-org** (``user_id`` provided): processes exactly one org
  and returns the number of blobs cleaned (legacy contract).

Idempotent: deleting already-deleted keys from S3 is a no-op (S3 does
not error on ``DeleteObject`` for missing keys).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from sqlalchemy import select

from models.organization import Organization
from workers.tasks.base import with_retry, with_task_metrics

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


@with_task_metrics
@with_retry(max_retries=2, base_delay_s=2.0)
async def cleanup_orphan_blobs(
    ctx: object,
    *,
    user_id: str | None = None,
    project_id: str | None = None,
    episode_id: str | None = None,
    batch_size: int = 100,
    trace_id: str = "",
) -> int | dict:
    """Remove orphaned S3 blobs whose episodes are soft-deleted.

    When ``user_id`` is provided, processes a single org and returns the
    number of blobs cleaned (``int``).  When ``user_id`` is ``None``
    (scheduled cron), discovers all orgs and returns a summary dict
    (``{"status", "orgs_processed", "orgs_failed", "blobs_cleaned"}``).

    Pipeline:
        1. Open DB session with RLS context.
        2. Fetch orphaned blob records (episode.is_deleted = true).
        3. Delete blobs from the global S3 backend.
        4. Delete blob DB records.

    Args:
        ctx: ARQ worker context.
        user_id: Organization UUID string.  ``None`` (cron) processes all
            orgs.
        project_id: Optional project UUID to scope cleanup.  Unused —
            retained for API parity with the memory deletion flow.
        episode_id: Optional episode UUID to target a specific episode.
        batch_size: Max blobs to process in one run (default 100).
        trace_id: Request trace ID for correlation.

    Returns:
        Number of blobs cleaned (single-org mode), or a summary dict with
        ``status`` (``completed``/``partial``/``skipped``),
        ``orgs_processed``, ``orgs_failed``, and ``blobs_cleaned``.

    Raises:
        RuntimeError: If discovery mode is used and every org fails.
    """
    if trace_id:
        structlog.contextvars.bind_contextvars(trace_id=trace_id)

    log = logger.bind(user_id=user_id)
    log.info("cleanup_orphan_blobs.start")

    # Lazy imports — ARQ workers run in separate process.
    from core import rls
    from core.config import settings
    from core.db import get_async_session
    from repositories.episode_blob_repository import EpisodeBlobRepository

    engine = ctx.get("db_engine") if isinstance(ctx, dict) else None
    if engine is None:
        from core.db import init_db_engine

        engine = init_db_engine(str(settings.DATABASE_URL), pool_size=2, max_overflow=1)
        _own_engine = True
    else:
        _own_engine = False
    session_factory = ctx.get("db_session_factory") if isinstance(ctx, dict) else None
    if session_factory is None:
        session_factory = get_async_session(engine)

    async def _cleanup_for_org(
        db: AsyncSession,
        org_uuid: UUID,
    ) -> int:
        """Remove orphaned blobs for a single org within the open session.

        Args:
            db: Open DB session (RLS ``app.user_id`` set per call).
            org_uuid: Organization UUID.

        Returns:
            Number of blobs cleaned for this org.
        """
        user_id = str(org_uuid)
        org_log = logger.bind(user_id=user_id)
        org_log.info("cleanup_orphan_blobs.org_start")

        # Bind the tenant on the session.  ``rls.set_user_context`` is
        # remembered by the session, so the per-org commit below re-applies
        # it — a bare ``set_config`` would leave the session un-tenanted
        # after that commit.  The discovery scan above ran under a bypass;
        # the per-org work drops it and runs tenant-scoped.
        await rls.set_bypass_rls(db, False)
        await rls.set_user_context(db, org_uuid)

        blob_repo = EpisodeBlobRepository(db)

        try:
            # Fetch orphans
            if episode_id:
                blobs = await blob_repo.get_by_episode(UUID(episode_id))
            else:
                blobs = await blob_repo.get_orphaned_blobs(
                    org_uuid, limit=batch_size,
                )

            if not blobs:
                org_log.info("cleanup_orphan_blobs.nothing_to_do")
                return 0

            org_log.info("cleanup_orphan_blobs.found", count=len(blobs))

            # Global backend — fail-fast Settings, never per-org.
            from core.blob_storage import BlobStorage, BlobStorageConfig
            from core.config import get_settings
            from services.blob_storage_service import BlobStorageService

            svc = BlobStorageService(db, blob_repo)
            storage = BlobStorage(BlobStorageConfig.from_settings(get_settings()))

            # S3 delete raises on failure — DB rows are deleted only after
            # S3 confirms, so a failed job retries with state intact instead
            # of dropping DB rows for bytes that still exist.
            await svc.delete_blobs(blobs, storage=storage)

            # Delete from DB
            blob_ids = [b.id for b in blobs]
            deleted = await blob_repo.delete_by_ids(blob_ids)

            await db.commit()
        except Exception:
            # The session is shared across orgs in discovery mode: a failed
            # statement aborts the transaction, and any later statement on
            # this session would raise InFailedSqlTransaction.  Roll back so
            # the next org starts from a clean transaction — and so this
            # org's pending deletes cannot be flushed by a later commit.
            await db.rollback()
            raise
        org_log.info(
            "cleanup_orphan_blobs.complete",
            deleted=len(deleted),
        )
        return len(deleted)

    try:
        async with session_factory() as db:
            if user_id is None:
                # ── Scheduled cron mode: discover all orgs ───────────────
                # The organizations RLS policy (migrations/0001) requires
                # app.bypass_rls='true' OR a matching app.user_id — the cron
                # sets neither, so without this the discovery would return
                # zero rows under any RLS-enforced role and the S3 leak
                # would silently continue.  Safe to bypass: get_orphaned_blobs
                # filters by org, and the blob IDs it returns are the same
                # org-scoped IDs passed to delete_by_ids in this iteration
                # (provenance).  The bypass is bound on the session (so it
                # survives the per-org commit); the per-org work drops it
                # again and binds the tenant instead.
                await rls.set_bypass_rls(db, True)
                result = await db.execute(select(Organization.id))
                org_ids = [r[0] for r in result.all()]

                if not org_ids:
                    return {
                        "status": "skipped",
                        "reason": "No organizations found",
                        "orgs_processed": 0,
                        "orgs_failed": 0,
                        "blobs_cleaned": 0,
                    }

                org_errors: list[str] = []
                total_cleaned = 0
                for org_uuid in org_ids:
                    try:
                        cleaned = await _cleanup_for_org(
                            db, org_uuid,
                        )
                        total_cleaned += cleaned
                    except Exception as exc:
                        log.error(
                            "cleanup_orphan_blobs.org_failed",
                            user_id=str(org_uuid),
                            error=str(exc),
                        )
                        org_errors.append(str(org_uuid))

                if org_errors and len(org_errors) == len(org_ids):
                    raise RuntimeError(
                        f"All {len(org_ids)} orgs failed: {', '.join(org_errors)}"
                    )

                return {
                    "status": "completed" if not org_errors else "partial",
                    "orgs_processed": len(org_ids),
                    "orgs_failed": len(org_errors),
                    "blobs_cleaned": total_cleaned,
                }

            # ── Manual mode: single org, legacy int contract ─────────────
            return await _cleanup_for_org(
                db, UUID(user_id),
            )

    except Exception:
        log.exception("cleanup_orphan_blobs.failed")
        raise
    finally:
        if _own_engine:
            await engine.dispose()
