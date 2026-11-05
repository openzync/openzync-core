"""Memory service — facade composing focused ingest collaborators.

Sub-package layout (one responsibility per module)::

    ingest.py         orchestration + session/user resolution
    dedup_claim.py    idempotency check + atomic content-hash claim
    pii_redaction.py  fail-closed PII config + redaction
    episode_write.py  row lock + sequence assignment + batch insert
    blob_ingest.py    S3 upload + compensation + extraction enqueue
    enqueue_tasks.py  idempotent ARQ fan-out + context-cache purge
    project_wipe.py   GDPR project memory wipe

``MemoryService`` keeps the original import path and public API
(``ingest``, ``delete_project_memory``); routers and factories are
unchanged. ``services/memory_service.py`` re-exports it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from repositories.episode_blob_repository import EpisodeBlobRepository
from repositories.episode_repository import EpisodeRepository
from repositories.fact_repository import FactRepository
from repositories.ingest_dedup_repository import IngestDedupRepository
from repositories.organization_repository import OrganizationRepository
from repositories.project_repository import ProjectRepository
from repositories.session_repository import SessionRepository
from repositories.user_repository import UserRepository
from services.idempotency_service import IdempotencyService
from services.memory.blob_ingest import BlobIngestMixin
from services.memory.dedup_claim import DedupClaimMixin
from services.memory.enqueue_tasks import ARQ_QUEUE, ARQ_TASKS, EnqueueTasksMixin
from services.memory.episode_write import EpisodeWriteMixin
from services.memory.ingest import IngestMixin
from services.memory.per_episode import PerEpisodeEnqueueMixin
from services.memory.pii_redaction import PIIRedactionMixin
from services.memory.project_wipe import ProjectWipeMixin
from services.webhook_service import WebhookService

if TYPE_CHECKING:
    from redis.asyncio import Redis as AsyncRedis
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class MemoryService(
    IngestMixin,
    DedupClaimMixin,
    PIIRedactionMixin,
    EpisodeWriteMixin,
    BlobIngestMixin,
    EnqueueTasksMixin,
    PerEpisodeEnqueueMixin,
    ProjectWipeMixin,
):
    """Service layer for message ingestion and memory management.

    ``org_id`` is passed as a parameter to ``ingest()`` and
    ``delete_user_memory()``, not stored on the instance — every public
    method explicitly accepts tenant context for auditability.

    Args:
        db: An async SQLAlchemy session (request-scoped).
        redis_client: An async Redis client for caching and idempotency.
        episode_repo: Repository for episode CRUD.
        session_repo: Repository for session CRUD.
        user_repo: Repository for user CRUD.
        fact_repo: Repository for fact CRUD (used in memory wipe).
    """

    def __init__(
        self,
        db: AsyncSession,
        redis_client: AsyncRedis,
        episode_repo: EpisodeRepository | None = None,
        session_repo: SessionRepository | None = None,
        user_repo: UserRepository | None = None,
        fact_repo: FactRepository | None = None,
        webhook_service: WebhookService | None = None,
        org_repo: OrganizationRepository | None = None,
        blob_repo: EpisodeBlobRepository | None = None,
        idempotency_service: IdempotencyService | None = None,
        dedup_repo: IngestDedupRepository | None = None,
        bao_client: Any | None = None,
        project_repo: ProjectRepository | None = None,
    ) -> None:
        self._db = db
        self._redis = redis_client
        self._webhook_service = webhook_service
        self._idem = idempotency_service or IdempotencyService(redis_client)
        self._bao_client = bao_client

        # Repositories (injected or auto-created)
        self._episode_repo = episode_repo or EpisodeRepository(db)
        self._session_repo = session_repo or SessionRepository(db)
        self._user_repo = user_repo or UserRepository(db)
        self._fact_repo = fact_repo or FactRepository(db)
        self._org_repo = org_repo or OrganizationRepository(db)
        self._blob_repo = blob_repo or EpisodeBlobRepository(db)
        self._dedup_repo = dedup_repo or IngestDedupRepository(db)
        self._project_repo = project_repo or ProjectRepository(db)


__all__ = ["ARQ_QUEUE", "ARQ_TASKS", "MemoryService"]
