"""Project wipe — GDPR soft-delete of episodes + facts with S3 sweep."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from core.exceptions import NotFoundError, ValidationError

logger = logging.getLogger(__name__)


class ProjectWipeMixin:
    """Memory-wipe operation for a project."""

    _db: Any
    _project_repo: Any
    _episode_repo: Any
    _fact_repo: Any
    _blob_repo: Any

    async def delete_project_memory(
        self,
        org_id: UUID,
        project_id: UUID,
        *,
        confirm: str,
        actor_id: UUID,
    ) -> tuple[int, int]:
        """Soft-delete all memory (episodes + facts) for a project.

        This is the GDPR / memory-wipe operation for a project. It does
        **not** delete sessions — only the data within them. The wipe only
        proceeds when ``confirm`` exactly equals the target project ID;
        anything else is rejected before any write. A successful wipe is
        recorded as a destructive-action audit log entry.

        Args:
            org_id: The authenticated organization UUID.
            project_id: The project UUID.
            confirm: Must equal ``str(project_id)`` — proves intent.
            actor_id: The authenticated user's UUID (audit attribution).

        Returns:
            Tuple of ``(episodes_deleted, facts_deleted)`` counts.

        Raises:
            ValidationError: If ``confirm`` does not match ``project_id``
                (→ 422, nothing is deleted).
            NotFoundError: If the project does not exist in this
                organization or is archived (→ 404, nothing is deleted).
                # ⚠️ BREAKING: cross-org wipes previously deleted by
                # project_id alone; they are now rejected.
                # Storage-config failures raise (→ 502): rows are already
                # committed, but the S3 sweep must never be silently
                # skipped — leftovers stay visible to the orphan-blob GC
                # worker via the soft-deleted episodes. Per-page S3
                # failures below stay best-effort (log + stop the sweep).
        """
        if confirm != str(project_id):
            raise ValidationError(
                "confirm does not match project_id — memory wipe rejected"
            )

        # ⚠️ Org gate: the wipe must not touch a project outside the
        # caller's organization. get_by_id is org-scoped and excludes
        # archived projects, so cross-org/archived/missing all 404 here.
        project = await self._project_repo.get_by_id(org_id, project_id)
        if project is None:
            raise NotFoundError(
                message=f"Project {project_id} not found",
                detail={"project_id": str(project_id)},
            )

        episodes_deleted = await self._episode_repo.soft_delete_by_project(
            project_id, org_id
        )
        facts_deleted = await self._fact_repo.soft_delete_by_project(
            project_id, org_id
        )

        # ── Commit before touching S3 ───────────────────────────────────
        # Rows are durable now. Every S3 delete below runs post-commit: a
        # crash leaves blob rows pointing at live objects with soft-deleted
        # episodes, which cleanup_orphan_blobs retries (S3 + row delete).
        await self._db.commit()

        # Post-commit S3 sweep — a config failure above raises loudly;
        # per-page deletes stay best-effort. Blob rows are deliberately
        # left for the orphan-blob GC worker, so a failure here only logs
        # loudly and stops the sweep — leftovers stay GC-visible instead
        # of becoming invisible S3 leaks.
        from core.blob_storage import BlobStorage, BlobStorageConfig
        from services.blob_storage_service import BlobStorageService

        blobs_deleted = 0
        last_id: UUID | None = None
        try:
            storage_config = await self._resolve_storage_config(org_id)
        except Exception:
            logger.exception(
                "memory.wipe_storage_config_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                },
            )
            raise
        # One S3 client for the whole sweep — not one per page.
        storage = BlobStorage(BlobStorageConfig.from_org_config(storage_config))
        blob_svc = BlobStorageService(self._db, self._blob_repo)
        while True:
            page = await self._blob_repo.get_by_project_after(
                project_id, last_id, limit=500
            )
            if not page:
                break
            last_id = page[-1].id
            try:
                await blob_svc.delete_blobs(page, storage_config, storage=storage)
            except Exception:
                logger.exception(
                    "memory.wipe_blob_delete_failed",
                    extra={
                        "org_id": str(org_id),
                        "project_id": str(project_id),
                        "page_size": len(page),
                    },
                )
                break
            blobs_deleted += len(page)

        logger.info(
            "memory.project_memory_deleted",
            extra={
                "project_id": str(project_id),
                "org_id": str(org_id),
                "episodes_deleted": episodes_deleted,
                "facts_deleted": facts_deleted,
                "blobs_deleted": blobs_deleted,
            },
        )

        from services.audit_log_service import AuditLogService

        await AuditLogService(self._db).log_action(
            organization_id=org_id,
            actor_id=str(actor_id),
            actor_type="user",
            action="memory.wipe",
            resource_type="memory",
            resource_id=str(project_id),
            details={
                "project_id": str(project_id),
                "confirm": "matched",
                "timestamp": datetime.now(UTC).isoformat(),
                "episodes_deleted": episodes_deleted,
                "facts_deleted": facts_deleted,
            },
        )

        return episodes_deleted, facts_deleted
