"""Cleanup worker — removes orphaned S3 blobs for soft-deleted episodes.

Runs as a low-priority scheduled ARQ task (daily 03:00 UTC cron) and can
also be triggered manually per-org.

Two modes:

* **Scheduled / org-discovery** (``org_id=None``): discovers all
  organization IDs via ``select(Organization.id)`` and processes each org
  independently, collecting per-org failures into a summary dict.
* **Manual / single-org** (``org_id`` provided): processes exactly one org
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
from workers.tasks.base import with_retry

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


@with_retry(max_retries=2, base_delay_s=2.0)
async def cleanup_orphan_blobs(
    ctx: object,
    *,
    org_id: str | None = None,
    project_id: str | None = None,
    episode_id: str | None = None,
    batch_size: int = 100,
    trace_id: str = "",
) -> int | dict:
    """Remove orphaned S3 blobs whose episodes are soft-deleted.

    When ``org_id`` is provided, processes a single org and returns the
    number of blobs cleaned (``int``).  When ``org_id`` is ``None``
    (scheduled cron), discovers all orgs and returns a summary dict
    (``{"status", "orgs_processed", "orgs_failed", "blobs_cleaned"}``).

    Pipeline:
        1. Open DB session with RLS context.
        2. Fetch orphaned blob records (episode.is_deleted = true).
        3. Resolve org storage config from OpenBao.
        4. Delete blobs from S3.
        5. Delete blob DB records.

    Args:
        ctx: ARQ worker context.
        org_id: Organization UUID string.  ``None`` (cron) processes all
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

    log = logger.bind(org_id=org_id)
    log.info("cleanup_orphan_blobs.start")

    # Lazy imports — ARQ workers run in separate process.
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

    bao_client = ctx.get("openbao_client") if isinstance(ctx, dict) else None

    async def _cleanup_for_org(
        db: AsyncSession,
        org_uuid: UUID,
        bao_client: object | None,
        session_factory: object,
    ) -> int:
        """Remove orphaned blobs for a single org within the open session.

        Args:
            db: Open DB session (RLS ``app.org_id`` set per call).
            org_uuid: Organization UUID.
            bao_client: Authenticated OpenBao client from the ARQ context,
                or ``None`` (falls back to BootstrapSettings).
            session_factory: Session factory from the ARQ context (kept
                for the documented helper contract — the session is
                already open via ``db``).

        Returns:
            Number of blobs cleaned for this org.
        """
        org_id = str(org_uuid)
        org_log = logger.bind(org_id=org_id)
        org_log.info("cleanup_orphan_blobs.start")

        from sqlalchemy import text

        await db.execute(
            text("SELECT set_config('app.org_id', :oid, true)"),
            {"oid": org_id},
        )

        blob_repo = EpisodeBlobRepository(db)

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

        # Resolve org storage config
        storage_config: dict = {
            "backend": "s3",
            "endpoint_url": "http://minio:9000",
            "region": "auto",
            "access_key_id": "",
            "secret_access_key": "",
            "bucket_name": "openzync-blobs",
            "max_blob_size_mb": 50,
        }
        try:
            if bao_client is not None:
                from core.org_config import get_org_config

                org_cfg = await get_org_config(
                    org_uuid, redis=None, bao_client=bao_client,
                )
                org_storage = org_cfg.to_blob_storage_config()
                if org_storage:
                    storage_config.update(org_storage)
            else:
                from core.config import BootstrapSettings
                from core.openbao import OpenBaoClient
                from core.org_config import get_org_config

                bootstrap = BootstrapSettings()
                async with OpenBaoClient(
                    bootstrap.OPENBAO_ADDR,
                    bootstrap.OPENBAO_ROLE_ID,
                    bootstrap.OPENBAO_SECRET_ID,
                    timeout=10.0,
                ) as _tmp_bao:
                    org_cfg = await get_org_config(
                        org_uuid, redis=None, bao_client=_tmp_bao,
                    )
                    org_storage = org_cfg.to_blob_storage_config()
                    if org_storage:
                        storage_config.update(org_storage)
        except Exception:
            org_log.warning(
                "cleanup_orphan_blobs.config_fetch_failed",
                exc_info=True,
            )

        from services.blob_storage_service import BlobStorageService

        svc = BlobStorageService(db, blob_repo)

        # Delete from S3 (best-effort)
        await svc.delete_blobs(blobs, storage_config)

        # Delete from DB
        blob_ids = [b.id for b in blobs]
        deleted = await blob_repo.delete_by_ids(blob_ids)

        await db.commit()
        org_log.info(
            "cleanup_orphan_blobs.complete",
            deleted=len(deleted),
        )
        return len(deleted)

    try:
        async with session_factory() as db:
            if org_id is None:
                # ── Scheduled cron mode: discover all orgs ───────────────
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
                            db, org_uuid, bao_client, session_factory,
                        )
                        total_cleaned += cleaned
                    except Exception as exc:
                        log.error(
                            "cleanup_orphan_blobs.org_failed",
                            org_id=str(org_uuid),
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
                db, UUID(org_id), bao_client, session_factory,
            )

    except Exception:
        log.exception("cleanup_orphan_blobs.failed")
        raise
    finally:
        if _own_engine:
            await engine.dispose()
