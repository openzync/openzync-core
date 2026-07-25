"""Cleanup worker — removes orphaned S3 blobs for soft-deleted episodes.

Runs as a low-priority scheduled ARQ task.  Also invoked inline when
``DELETE /v1/projects/{id}/memory`` is called.

Idempotent: deleting already-deleted keys from S3 is a no-op (S3 does
not error on ``DeleteObject`` for missing keys).
"""

from __future__ import annotations

from uuid import UUID

import structlog

from workers.tasks.base import with_retry

logger = structlog.get_logger(__name__)


@with_retry(max_retries=2, base_delay_s=2.0)
async def cleanup_orphan_blobs(
    ctx: object,
    *,
    org_id: str,
    project_id: str | None = None,
    episode_id: str | None = None,
    batch_size: int = 100,
    trace_id: str = "",
) -> int:
    """Remove orphaned S3 blobs whose episodes are soft-deleted.

    Pipeline:
        1. Open DB session with RLS context.
        2. Fetch orphaned blob records (episode.is_deleted = true).
        3. Resolve org storage config from OpenBao.
        4. Delete blobs from S3.
        5. Delete blob DB records.

    Args:
        ctx: ARQ worker context.
        org_id: Organization UUID string.
        project_id: Optional project UUID to scope cleanup.
        episode_id: Optional episode UUID to target a specific episode.
        batch_size: Max blobs to process in one run (default 100).
        trace_id: Request trace ID for correlation.

    Returns:
        Number of blobs cleaned up.
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

    try:
        async with session_factory() as db:
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
                    UUID(org_id), limit=batch_size,
                )

            if not blobs:
                log.info("cleanup_orphan_blobs.nothing_to_do")
                return 0

            log.info("cleanup_orphan_blobs.found", count=len(blobs))

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
                        UUID(org_id), redis=None, bao_client=bao_client,
                    )
                    org_storage = org_cfg.to_blob_storage_config()
                    if org_storage:
                        storage_config.update(org_storage)
                else:
                    from core.config import BootstrapSettings
                    from core.openbao import OpenBaoClient

                    bootstrap = BootstrapSettings()
                    async with OpenBaoClient(
                        bootstrap.OPENBAO_ADDR,
                        bootstrap.OPENBAO_ROLE_ID,
                        bootstrap.OPENBAO_SECRET_ID,
                        timeout=10.0,
                    ) as _tmp_bao:
                        org_cfg = await get_org_config(
                            UUID(org_id), redis=None, bao_client=_tmp_bao,
                        )
                        org_storage = org_cfg.to_blob_storage_config()
                        if org_storage:
                            storage_config.update(org_storage)
            except Exception:
                log.warning(
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
            log.info(
                "cleanup_orphan_blobs.complete",
                deleted=len(deleted),
            )
            return len(deleted)

    except Exception:
        log.exception("cleanup_orphan_blobs.failed")
        raise
    finally:
        if _own_engine:
            await engine.dispose()
