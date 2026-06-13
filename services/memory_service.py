"""Memory service — business logic for message ingestion and memory management.

This is the primary entry point for persisting agent memory. The service:

1. Resolves or creates users and sessions
2. Validates and persists messages as episodes in PostgreSQL
3. Enqueues ARQ worker tasks for async enrichment (entity extraction,
   embedding, fact extraction, graph sync)
4. Manages idempotency and content-level deduplication via Redis
5. Supports full memory wipe (soft-delete all episodes + facts)

Separation: service orchestrates, repositories query. No SQLAlchemy
expressions in this file.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from redis.asyncio import Redis as AsyncRedis

    from models.session import Session
    from models.user import User

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.arq import get_arq
from core.config import settings
from core.exceptions import NotFoundError, ValidationError
from repositories.episode_repository import EpisodeRepository
from repositories.fact_repository import FactRepository
from repositories.session_repository import SessionRepository
from repositories.user_repository import UserRepository
from schemas.memory import IngestMemoryResponse, Message

logger = logging.getLogger(__name__)

# ╠ This file contains NO SQLAlchemy expressions.
# ╠ If you see a ``select()`` or ``where()``, it belongs in the repository.

# ── Constants ────────────────────────────────────────────────────────────────

IDEMPOTENCY_TTL = 172800  # 48 hours
"""TTL for idempotency key and content-hash cache entries (seconds)."""

CONTENT_HASH_PREFIX = "contenthash:"
"""Redis key prefix for content-dedup hash entries."""

IDEMPOTENCY_PREFIX = "idempotency:"
"""Redis key prefix for idempotency-key cache entries."""

CONTEXT_CACHE_PATTERN = "ctx:{org_id}:{user_id}:*"
"""Redis key pattern for context cache entries to invalidate."""

ARQ_TASKS = [
    "classify_dialog",
    "sync_to_graph",
    "extract_entities",
    "extract_facts",
    "embed_episode",
    "extract_structured",
]
"""ARQ worker task names enqueued after a successful ingestion."""

ARQ_QUEUE = "high"
"""ARQ queue name for ingestion-related background tasks."""


def _arq_queue_name(queue_type: str) -> str:
    """Build the full ARQ queue name matching the worker's config.

    Worker uses: ``get_queue_name(settings.ENVIRONMENT, queue_type)``
    which produces: ``OpenZep:{env}:queue:{queue_type}``

    Args:
        queue_type: Queue type suffix (e.g. ``"high"``, ``"low"``).

    Returns:
        Fully qualified queue name for the current environment.
    """
    env = settings.ENVIRONMENT if hasattr(settings, "ENVIRONMENT") else "development"
    return f"OpenZep:{env}:queue:{queue_type}"


class MemoryService:
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
    ) -> None:
        self._db = db
        self._redis = redis_client

        # Repositories (injected or auto-created)
        self._episode_repo = episode_repo or EpisodeRepository(db)
        self._session_repo = session_repo or SessionRepository(db)
        self._user_repo = user_repo or UserRepository(db)
        self._fact_repo = fact_repo or FactRepository(db)

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    async def ingest(
        self,
        org_id: UUID,
        user_uuid: UUID,
        session_external_id: str | None,
        messages: list[Message],
        idempotency_key: str | None = None,
    ) -> IngestMemoryResponse:
        """Ingest messages into a user's memory.

        Flow:
        1. Idempotency check (Redis) — return cached response if duplicate.
        2. Resolve or auto-create the user via ``get_or_create``.
        3. Resolve or auto-create the session (``__default__`` if omitted).
        4. Compute content hash for content-level dedup.
        5. Get next sequence number for ordered insertion.
        6. Build episode dicts from validated messages.
        7. PII detection & redaction (if enabled in org quotas) — mask or
           block based on org config.
        8. Batch-insert episodes into PostgreSQL.
        9. Enqueue ARQ enrichment tasks (sync_to_graph, extract_entities,
           extract_facts, embed_episode).
        10. Cache idempotency key and content hash for future dedup.
        11. Invalidate context cache for this user.
        12. Return 202 ``IngestMemoryResponse``.

        Args:
            org_id: The authenticated organization UUID.
            user_external_id: Caller-defined user identifier.
            session_external_id: Optional session external ID.
                Auto-creates ``__default__`` if omitted.
            messages: List of validated message objects.
            idempotency_key: Optional ``Idempotency-Key`` header value
                for request-level deduplication.

        Returns:
            An ``IngestMemoryResponse`` with job_id and episode count.

        Raises:
            NotFoundError: If the user does not exist and cannot be created
                (should never happen — get_or_create always succeeds).
        """
        # ── Step 1: Idempotency check ────────────────────────────────────
        if idempotency_key is not None:
            cached = await self._check_idempotency(idempotency_key)
            if cached is not None:
                logger.info(
                    "memory.idempotency_replay",
                    extra={"idempotency_key": idempotency_key, "org_id": str(org_id)},
                )
                return cached

        # ── Step 2: Resolve user by UUID ─────────────────────────────────
        user = await self._user_repo.get_by_uuid(org_id, user_uuid)
        if user is None:
            raise NotFoundError(f"User {user_uuid} not found in organization {org_id}")
        user_id = user.id
        logger.debug(
            "memory.user_resolved",
            extra={
                "user_id": str(user_id),
                "org_id": str(org_id),
            },
        )

        # ── Step 3: Resolve or create session ────────────────────────────
        session = await self._resolve_session(
            organization_id=org_id,
            user_id=user_id,
            session_external_id=session_external_id,
        )
        session_id = session.id
        logger.debug(
            "memory.session_resolved",
            extra={
                "session_id": str(session_id),
                "external_id": session.external_id,
                "user_id": str(user_id),
            },
        )

        # ── Step 4: Content-level dedup ──────────────────────────────────
        content_hash = self._compute_content_hash(
            user_id=str(user_id),
            session_id=str(session_id),
            messages=messages,
        )
        existing_job_id = await self._check_content_dedup(content_hash)
        if existing_job_id is not None:
            logger.info(
                "memory.content_dedup_hit",
                extra={
                    "content_hash": content_hash,
                    "existing_job_id": existing_job_id,
                    "user_id": str(user_id),
                },
            )
            return IngestMemoryResponse(
                job_id=existing_job_id,
                episode_count=len(messages),
                status="accepted",
                message="Content already ingested; returning existing job_id",
            )

        # ── Step 5: Get next sequence number ──────────────────────────────
        # Compute the starting sequence number so episodes are ordered
        # correctly even if multiple batches arrive concurrently.
        start_seq = await self._episode_repo.get_next_sequence(session_id)

        # ── Step 6: Build episode dicts ───────────────────────────────────
        episode_dicts = [
            {
                "role": msg.role,
                "content": msg.content,
                "metadata": msg.metadata,
                "created_at": msg.created_at,
                "sequence_number": start_seq + i,
            }
            for i, msg in enumerate(messages)
        ]

        # ── Step 7: PII detection & redaction ─────────────────────────────
        pii_config_raw = await self._get_org_pii_config(org_id)
        pii_mode = (
            pii_config_raw.get("mode", "off")
            if isinstance(pii_config_raw, dict)
            else "off"
        )

        if pii_mode != "off":
            from services.pii_service import PIIService

            pii_service = PIIService(pii_config_raw)
            for msg_dict in episode_dicts:
                content = msg_dict["content"]
                redacted, detections, was_blocked = (
                    await pii_service.process_message(content)
                )

                # In block mode, process_message raises ValidationError
                # so we only reach here in mask mode
                if redacted != content:
                    msg_dict["content"] = redacted

        # ── Step 8: Batch-insert episodes ────────────────────────────────
        episodes = await self._episode_repo.batch_create(
            organization_id=org_id,
            session_id=session_id,
            user_id=user_id,
            messages=episode_dicts,
        )
        episode_ids = [ep.id for ep in episodes]
        logger.info(
            "memory.episodes_created",
            extra={
                "count": len(episodes),
                "session_id": str(session_id),
                "user_id": str(user_id),
                "org_id": str(org_id),
            },
        )

        # ── Step 8: Generate job_id and enqueue ARQ tasks ────────────────
        job_id = str(uuid4())
        episode_dicts = [
            {"id": ep.id, "content": ep.content, "role": ep.role}
            for ep in episodes
        ]
        # Extract project_id from the session (always populated after migration)
        project_id = str(session.project_id) if session.project_id else str(org_id)
        await self._enqueue_arq_tasks(
            job_id=job_id,
            org_id=str(org_id),
            project_id=project_id,
            user_id=str(user_id),
            session_id=str(session_id),
            episodes=episode_dicts,
        )

        # ── Step 9: Cache idempotency key and content hash ───────────────
        response = IngestMemoryResponse(
            job_id=job_id,
            episode_count=len(episodes),
            status="accepted",
            message="Messages accepted for processing",
        )

        if idempotency_key is not None:
            await self._cache_idempotency(idempotency_key, response)

        await self._cache_content_hash(content_hash, job_id)

        # ── Step 10: Invalidate context cache for this user ──────────────
        await self._invalidate_context_cache(str(org_id), str(user_id))

        return response

    async def delete_user_memory(
        self,
        org_id: UUID,
        user_uuid: UUID,
    ) -> tuple[int, int]:
        """Soft-delete all memory (episodes + facts) for a user.

        This is the GDPR / memory-wipe operation. It does **not** delete
        the user or their sessions — only the data within them.

        Args:
            org_id: The authenticated organization UUID.
            user_uuid: The internal user UUID.

        Returns:
            Tuple of ``(episodes_deleted, facts_deleted)`` counts.

        Raises:
            NotFoundError: If the user does not exist.
        """
        user = await self._user_repo.get_by_uuid(org_id, user_uuid)
        if user is None:
            raise NotFoundError(
                f"User '{user_uuid}' not found in organization {org_id}"
            )

        episodes_deleted = await self._episode_repo.soft_delete_by_user(user.id)
        facts_deleted = await self._fact_repo.soft_delete_by_user(user.id)

        logger.info(
            "memory.user_memory_deleted",
            extra={
                "user_id": str(user.id),
                "user_uuid": str(user_uuid),
                "org_id": str(org_id),
                "episodes_deleted": episodes_deleted,
                "facts_deleted": facts_deleted,
            },
        )

        return episodes_deleted, facts_deleted

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────────

    async def _resolve_user(
        self,
        org_id: UUID,
        external_id: str,
    ) -> User:
        """Resolve a user by external_id, creating one if it does not exist.

        Thread-safe via the ``(organization_id, external_id)`` unique
        constraint — concurrent creates are handled with an IntegrityError
        retry in the repository layer.

        Args:
            org_id: The organization UUID.
            external_id: The caller-defined user identifier.

        Returns:
            A ``User`` ORM instance (existing or newly created).
        """
        user = await self._user_repo.get_by_external_id(org_id, external_id)
        if user is not None:
            return user

        # Race-safe: unique constraint prevents duplicate inserts
        from sqlalchemy.exc import IntegrityError

        try:
            user = await self._user_repo.create(
                organization_id=org_id,
                external_id=external_id,
            )
        except IntegrityError:
            await self._user_repo.rollback()
            user = await self._user_repo.get_by_external_id(org_id, external_id)
            if user is None:
                raise NotFoundError(
                    f"Failed to get-or-create user '{external_id}' "
                    f"in organization {org_id}"
                ) from None

        return user

    async def _resolve_session(
        self,
        organization_id: UUID,
        user_id: UUID,
        session_external_id: str | None,
    ) -> Session:
        """Resolve an existing session or auto-create a default one.

        Rules:
        - If ``session_external_id`` is provided: look up the existing
          session and raise ``NotFoundError`` if it does not exist.
          Sessions are NOT auto-created from arbitrary IDs — the SDK
          must call ``POST /sessions`` first.
        - If ``session_external_id`` is ``None``: get or create a session
          named ``__default__``. Uses ``INSERT ... ON CONFLICT DO NOTHING``
          for race safety.

        Args:
            organization_id: The organization UUID.
            user_id: The owning user's UUID.
            session_external_id: The caller-defined session identifier,
                or ``None`` to use the default session.

        Returns:
            A ``Session`` ORM instance.

        Raises:
            NotFoundError: If a specific session_id was given but not found.
        """
        from models.session import Session

        if session_external_id is not None:
            session = await self._session_repo.get_by_external_id(
                org_id=organization_id,
                user_id=user_id,
                external_id=session_external_id,
            )
            if session is None:
                raise NotFoundError(
                    f"Session '{session_external_id}' not found for user {user_id}"
                )
            return session

        # Auto-create or get existing "__default__" session
        return await self._session_repo.get_or_create_default(
            org_id=organization_id,
            user_id=user_id,
        )

    # ── Idempotency ──────────────────────────────────────────────────────────

    async def _check_idempotency(
        self, key: str
    ) -> IngestMemoryResponse | None:
        """Check Redis for a cached response for this idempotency key.

        Args:
            key: The ``Idempotency-Key`` header value.

        Returns:
            The cached ``IngestMemoryResponse`` if found, or ``None``.
        """
        cached = await self._redis.get(f"{IDEMPOTENCY_PREFIX}{key}")
        if cached is None:
            return None
        data = json.loads(cached)
        return IngestMemoryResponse(**data)

    async def _cache_idempotency(
        self, key: str, response: IngestMemoryResponse
    ) -> None:
        """Cache the response for this idempotency key.

        Args:
            key: The ``Idempotency-Key`` header value.
            response: The response to cache.
        """
        await self._redis.setex(
            f"{IDEMPOTENCY_PREFIX}{key}",
            IDEMPOTENCY_TTL,
            response.model_dump_json(),
        )

    # ── Content Dedup ────────────────────────────────────────────────────────

    @staticmethod
    def _compute_content_hash(
        user_id: str,
        session_id: str,
        messages: list[Message],
    ) -> str:
        """Compute a SHA-256 hash of (user_id, session_id, messages).

        Used for content-level deduplication: identical payloads from
        different clients produce the same hash and return the same job_id.

        Args:
            user_id: The user's UUID string.
            session_id: The session's UUID string.
            messages: The message list to hash.

        Returns:
            A hex-encoded SHA-256 digest.
        """
        canonical = json.dumps(
            {
                "user_id": user_id,
                "session_id": session_id,
                "messages": [
                    {
                        "role": m.role,
                        "content": m.content,
                        "metadata": m.metadata,
                    }
                    for m in messages
                ],
            },
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def _check_content_dedup(self, content_hash: str) -> str | None:
        """Check if this exact content has been ingested before.

        Args:
            content_hash: The SHA-256 content hash.

        Returns:
            The existing ``job_id`` if found, or ``None``.
        """
        existing = await self._redis.get(
            f"{CONTENT_HASH_PREFIX}{content_hash}"
        )
        return existing if existing else None

    async def _cache_content_hash(
        self, content_hash: str, job_id: str
    ) -> None:
        """Cache a content hash to prevent re-ingestion of identical content.

        Args:
            content_hash: The SHA-256 content hash.
            job_id: The job ID to associate with this content.
        """
        await self._redis.setex(
            f"{CONTENT_HASH_PREFIX}{content_hash}",
            IDEMPOTENCY_TTL,
            job_id,
        )

    # ── PII Config ────────────────────────────────────────────────────────────

    async def _get_org_pii_config(self, org_id: UUID) -> dict:
        """Fetch PII configuration for an org from their quotas JSONB.

        The PII config lives at ``organizations.quotas -> 'pii'``.  We use a
        raw ``text()`` query instead of a full repository to avoid scope creep —
        this is the only org-level query that ``MemoryService`` needs.

        Args:
            org_id: The organization UUID.

        Returns:
            The PII config dict (possibly empty).  Returns ``{}`` if the
            organization does not exist or has no PII config.
        """
        result = await self._db.execute(
            text("SELECT quotas->'pii' AS pii_config FROM organizations WHERE id = :org_id"),
            {"org_id": org_id},
        )
        row = result.one_or_none()
        if row is None:
            return {}
        pii_config = row[0]
        return pii_config if isinstance(pii_config, dict) else {}

    # ── ARQ Task Enqueue ─────────────────────────────────────────────────────

    async def _enqueue_arq_tasks(
        self,
        job_id: str,
        org_id: str,
        project_id: str,
        user_id: str,
        session_id: str,
        episodes: list[dict[str, Any]],
    ) -> None:
        """Enqueue ARQ background tasks for episode enrichment.

        Tasks are enqueued on the ``high`` priority queue:
        - ``sync_to_graph``: Populates Graphiti episodic nodes.
        - ``extract_entities``: LLM-based entity + relationship extraction.
        - ``extract_facts``: LLM-based zero-shot fact extraction.
        - ``embed_episode``: Generates embeddings via the configured API.

        One job per task per episode is enqueued. If the ARQ pool is
        unavailable (Redis down), episodes are safe in PostgreSQL and will
        be picked up by a reconciliation worker.

        Args:
            job_id: The composite job ID for this ingestion.
            org_id: The organization UUID string.
            project_id: The project UUID string for scoping graph entities.
            user_id: The user UUID string.
            session_id: The session UUID string.
            episodes: List of episode dicts with ``id``, ``content``, ``role``.
        """
        episode_ids = [ep["id"] for ep in episodes]
        try:
            arq_pool = get_arq()
            qname = _arq_queue_name("high")
            for episode in episodes:
                ep_id = str(episode["id"])
                content = episode["content"]
                role = episode.get("role", "user")
                common = {"episode_id": ep_id, "content": content, "org_id": org_id, "project_id": project_id}

                await arq_pool.enqueue("classify_dialog", queue_name=qname,
                    **common)
                await arq_pool.enqueue("extract_entities", queue_name=qname,
                    **common, user_id=user_id, session_id=session_id)
                await arq_pool.enqueue("extract_facts", queue_name=qname,
                    **common, user_id=user_id, session_id=session_id)
                await arq_pool.enqueue("embed_episode", queue_name=qname,
                    **common)
                await arq_pool.enqueue("sync_to_graph", queue_name=_arq_queue_name("low"),
                    **common, user_id=user_id, role=role)
                await arq_pool.enqueue("extract_structured", queue_name=qname,
                    **common, user_id=user_id, session_id=session_id)

            logger.info(
                "memory.arq_tasks_enqueued",
                extra={
                    "job_id": job_id,
                    "task_count": len(ARQ_TASKS),
                    "org_id": org_id,
                    "user_id": user_id,
                },
            )
        except Exception:
            # ⚠️ Episodes are already committed. ARQ failure does not
            # roll back the insert. A reconciliation worker will pick
            # up episodes without enrichment.
            logger.critical(
                "memory.arq_enqueue_failed",
                extra={
                    "job_id": job_id,
                    "org_id": org_id,
                    "user_id": user_id,
                    "session_id": session_id,
                    "episode_ids": episode_ids,
                    "error": "ARQ pool unavailable — tasks not enqueued. "
                    "Episodes are safe in PostgreSQL; reconciliation needed.",
                },
            )

    # ── Context Cache Invalidation ───────────────────────────────────────────

    async def _invalidate_context_cache(
        self, org_id: str, user_id: str
    ) -> None:
        """Invalidate all context cache entries for a user.

        Called after ingestion so that subsequent context-assembly
        queries fetch fresh data from the database.

        Uses Redis ``SCAN`` + ``DEL`` to match the pattern
        ``ctx:{org_id}:{user_id}:*``.

        Args:
            org_id: The organization UUID string.
            user_id: The user UUID string.
        """
        pattern = CONTEXT_CACHE_PATTERN.format(org_id=org_id, user_id=user_id)
        cursor: int = 0
        deleted = 0
        while True:
            cursor, keys = await self._redis.scan(
                cursor=cursor, match=pattern, count=100
            )
            if keys:
                deleted += await self._redis.delete(*keys)
            if cursor == 0:
                break
        if deleted > 0:
            logger.debug(
                "memory.context_cache_invalidated",
                extra={
                    "org_id": org_id,
                    "user_id": user_id,
                    "keys_deleted": deleted,
                },
            )

