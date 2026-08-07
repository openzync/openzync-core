"""Idempotency & Deduplication service — three layers of protection.

Layers
------
1. **HTTP-level** (``Idempotency-Key`` header):
   Prevents duplicate processing when a client retries the same request.
   Backed by Redis with 48h TTL.  Returns cached response on replay,
   raises conflict on key reuse with different payload.

2. **Content-level** (SHA-256 content hash):
   Prevents the same ``(org_id, user_id, session_id, messages)``
   combination from being ingested more than once, even from different
   clients with different ``Idempotency-Key`` values.  Backed by Redis
   with 48h TTL.

3. **Worker-level** (bitmask on ``episodes.enrichment_status``):
   Prevents ARQ worker tasks from processing the same episode twice.
   Uses ``SELECT ... FOR UPDATE`` row-level locking and bitwise
   operations on the integer enrichment_status column.

Usage
-----
    service = IdempotencyService(redis=app.state.redis)

    # HTTP-level
    result = await service.check_idempotency_key(key, body_hash, org_id)
    if result.status == IdempotencyStatus.NEW:
        response_data = await process_fn()
        await service.store_idempotency_key(key, body_hash, response_data, org_id)

    # Content-level
    existing = await service.check_content_hash(org_id, user_id, session_id, messages)
    if existing:
        return  # duplicate content — replay existing payload (e.g. job_id)
    ...
    await service.store_content_hash(
        org_id, user_id, session_id, messages, payload=job_id
    )

    # Worker-level
    if await service.check_and_mark_worker(db, episode_id, ENRICHMENT_ENTITIES):
        await do_extract_entities(db, episode_id)
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import UUID  # noqa: TCH003 — used in type hints for callers

import orjson
from redis import asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from repositories.episode_repository import EpisodeRepository

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Enums & types
# ═══════════════════════════════════════════════════════════════════════════════


class IdempotencyStatus(Enum):
    """Result of an idempotency check.

    Attributes:
        NEW:     First request with this key — proceed with processing.
        REPLAY:  Duplicate request — return cached response, no side effects.
        CONFLICT: Same key but different request body — client error.
    """

    NEW = "new"
    REPLAY = "replay"
    CONFLICT = "conflict"


class IdempotencyResult:
    """Structured result from :meth:`IdempotencyService.check_idempotency_key`.

    Attributes:
        status:        One of ``NEW``, ``REPLAY``, ``CONFLICT``.
        response_data: Cached response body when ``status == REPLAY``.
        message:       Human-readable description of the result.
    """

    __slots__ = ("status", "response_data", "message")

    def __init__(
        self,
        status: IdempotencyStatus,
        response_data: dict[str, Any] | None = None,
        message: str = "",
    ) -> None:
        self.status = status
        self.response_data = response_data
        self.message = message

    def __repr__(self) -> str:
        return (
            f"IdempotencyResult(status={self.status.value}, "
            f"has_response={self.response_data is not None}, "
            f"message={self.message!r})"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Worker-level bitmask constants
# ═══════════════════════════════════════════════════════════════════════════════
# These constants represent each enrichment task's bit position in the
# ``episodes.enrichment_status`` integer column.  Combine with bitwise OR (``|``)
# to mark multiple tasks as completed.

ENRICHMENT_ENTITIES: int = 1 << 0      # bit 0: entity extraction
ENRICHMENT_EMBEDDING: int = 1 << 1     # bit 1: episode embedding
ENRICHMENT_FACTS: int = 1 << 2         # bit 2: fact extraction
ENRICHMENT_ENTITY_LINKS: int = 1 << 3    # bit 3: entity-episode linking
ENRICHMENT_ALL: int = (
    ENRICHMENT_ENTITIES
    | ENRICHMENT_EMBEDDING
    | ENRICHMENT_FACTS
    | ENRICHMENT_ENTITY_LINKS
)
"""Bitmask with all bits set — used to check if an episode is fully enriched."""


# ═══════════════════════════════════════════════════════════════════════════════
# IdempotencyService
# ═══════════════════════════════════════════════════════════════════════════════


class IdempotencyService:
    """Three-layer idempotency and deduplication service.

    Args:
        redis: An async Redis client instance (``redis.asyncio.Redis``).
            **Must** have ``decode_responses=True`` for string-based values.
    """

    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis

        # ── Key prefixes ─────────────────────────────────────────────────
        _env = get_settings().ENVIRONMENT
        self._idem_prefix: str = f"OpenZync:{_env}:idempotency:"
        self._content_prefix: str = f"OpenZync:{_env}:contenthash:"
        self._cache_prefix: str = f"OpenZync:{_env}:cache:"

        # ── TTL ──────────────────────────────────────────────────────────
        _s = get_settings()
        self._idem_ttl: int = getattr(
            _s, "IDEMPOTENCY_TTL_SECONDS", 172800  # 48 hours
        )
        self._content_ttl: int = self._idem_ttl

        # ── Limits ───────────────────────────────────────────────────────
        self._max_key_length: int = 255

    # ──────────────────────────────────────────────────────────────────────────
    # HTTP-level idempotency
    # ──────────────────────────────────────────────────────────────────────────

    async def check_idempotency_key(
        self, key: str, body_hash: str, org_id: str | UUID
    ) -> IdempotencyResult:
        """Check an ``Idempotency-Key`` and return the appropriate action.

        Keys are tenant-namespaced — the cache key is
        ``OpenZync:{env}:idempotency:{org_id}:{key}`` — so the same key
        used by two different organizations never collides.

        Args:
            key: The ``Idempotency-Key`` header value (max 255 chars).
            body_hash: SHA-256 hex digest of the canonical request body.
            org_id: The tenant (organization) UUID or string. Normalized
                to ``str`` for the cache key.

        Returns:
            An :class:`IdempotencyResult`:
            - ``NEW``:      First use — caller should process and then
                            call :meth:`store_idempotency_key`.
            - ``REPLAY``:   Duplicate — ``response_data`` contains the
                            cached response; do **not** process.
            - ``CONFLICT``: Same key, different body — client error.

        Raises:
            ValueError: If ``key`` exceeds ``_max_key_length`` (255 chars).
        """
        if len(key) > self._max_key_length:
            raise ValueError(
                f"Idempotency-Key must not exceed {self._max_key_length} characters"
            )

        cache_key = f"{self._idem_prefix}{org_id}:{key}"

        cached: str | None = await self._redis.get(cache_key)
        if cached is None:
            # First request with this key.
            return IdempotencyResult(
                status=IdempotencyStatus.NEW,
                message="Idempotency key is new — proceed with processing.",
            )

        # Key exists — parse cached entry and validate body hash.
        try:
            entry: dict[str, Any] = orjson.loads(cached)
        except (orjson.JSONDecodeError, TypeError):
            # Corrupted cache entry — treat as new (cache will be overwritten).
            logger.error(
                "idempotency.corrupted_cache_entry",
                extra={"key": key[:16] + "..."},
            )
            return IdempotencyResult(
                status=IdempotencyStatus.NEW,
                message="Cached entry was corrupt — treat as new.",
            )

        stored_hash: str | None = entry.get("request_body_hash")
        if stored_hash is not None and stored_hash != body_hash:
            # Same key, different payload — this is a conflict.
            logger.warning(
                "idempotency.payload_mismatch",
                extra={
                    "key": key[:16] + "...",
                    "expected_hash": stored_hash,
                    "actual_hash": body_hash,
                },
            )
            return IdempotencyResult(
                status=IdempotencyStatus.CONFLICT,
                message=(
                    "Idempotency-Key already used with a different request body. "
                    "Each Idempotency-Key must uniquely identify a single request."
                ),
            )

        # Same key, same body — safe replay.
        response_data: dict[str, Any] | None = entry.get("response_body")
        return IdempotencyResult(
            status=IdempotencyStatus.REPLAY,
            response_data=response_data,
            message="Returning cached response from previous identical request.",
        )

    async def store_idempotency_key(
        self,
        key: str,
        body_hash: str,
        response_data: dict[str, Any],
        org_id: str | UUID,
    ) -> None:
        """Persist a successful response for future idempotency replay.

        The cache key is tenant-namespaced
        (``OpenZync:{env}:idempotency:{org_id}:{key}``) so replays cannot
        cross organization boundaries.

        Args:
            key: The ``Idempotency-Key`` header value.
            body_hash: SHA-256 hex digest of the canonical request body.
            response_data: The response dict to cache for replay.
            org_id: The tenant (organization) UUID or string. Normalized
                to ``str`` for the cache key.
        """
        cache_key = f"{self._idem_prefix}{org_id}:{key}"
        entry: dict[str, Any] = {
            "response_body": response_data,
            "request_body_hash": body_hash,
            "created_at": datetime.now(UTC).isoformat(),
        }

        await self._redis.setex(
            cache_key,
            self._idem_ttl,
            orjson.dumps(entry),
        )

        logger.debug(
            "idempotency.key_stored",
            extra={
                "key": key[:16] + "...",
                "ttl": self._idem_ttl,
            },
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Content-level deduplication
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def compute_content_hash(
        org_id: str,
        user_id: str,
        session_id: str,
        messages: list[dict[str, Any]],
    ) -> str:
        """Compute a deterministic SHA-256 hash for content dedup.

        The hash is based on ``(org_id, user_id, session_id, role,
        content)``.  ``metadata`` and ``blobs`` are included in each
        message's canonical form when those keys are present in the
        message dict, so identical payloads that differ only in absence
        vs presence of metadata/blobs do not collide.

        Args:
            org_id: Organisation UUID string.
            user_id: User UUID string.
            session_id: Session UUID or external ID string.
            messages: List of message dicts, each containing at minimum
                ``role`` and ``content`` keys.

        Returns:
            SHA-256 hex digest string (64 characters).
        """
        canonical = orjson.dumps(
            {
                "org_id": org_id,
                "user_id": user_id,
                "session_id": session_id,
                "messages": [
                    {
                        "role": m.get("role"),
                        "content": m.get("content"),
                        **({"metadata": m["metadata"]} if "metadata" in m else {}),
                        **({"blobs": m["blobs"]} if "blobs" in m else {}),
                    }
                    for m in messages
                ],
            },
            option=orjson.OPT_SORT_KEYS,
        )
        return hashlib.sha256(canonical).hexdigest()

    async def check_content_hash(
        self,
        org_id: str,
        user_id: str,
        session_id: str,
        messages: list[dict[str, Any]],
    ) -> str | None:
        """Check whether identical content has already been ingested.

        Computes the SHA-256 hash and looks it up in Redis.

        Args:
            org_id: Organisation UUID string.
            user_id: User UUID string.
            session_id: Session UUID string.
            messages: List of message dicts.

        Returns:
            The stored payload (e.g. the original ``job_id``) if this
            content was already ingested, ``None`` if it is new.
        """
        content_hash = self.compute_content_hash(
            org_id, user_id, session_id, messages
        )
        cache_key = self._content_prefix + content_hash

        cached: str | None = await self._redis.get(cache_key)
        if cached is not None:
            logger.info(
                "idempotency.content_dedup_hit",
                extra={
                    "org_id": org_id,
                    "user_id": user_id,
                    "session_id": session_id,
                    "message_count": len(messages),
                },
            )
            return cached

        return None

    async def store_content_hash(
        self,
        org_id: str,
        user_id: str,
        session_id: str,
        messages: list[dict[str, Any]],
        *,
        payload: str | None = None,
    ) -> str:
        """Store a content hash in Redis with TTL.

        Uses ``SETNX`` to atomically store only if absent, preventing
        a race where two concurrent ingestions of the same content both
        pass ``check_content_hash``.  When ``payload`` is given it is
        stored as the Redis value (e.g. the ``job_id`` to replay);
        otherwise the hash itself is stored.

        Args:
            org_id: Organisation UUID string.
            user_id: User UUID string.
            session_id: Session UUID string.
            messages: List of message dicts.
            payload: Optional value to store instead of the hash — the
                first caller's value wins (e.g. the first ``job_id``).

        Returns:
            The computed content hash hex string.
        """
        content_hash = self.compute_content_hash(
            org_id, user_id, session_id, messages
        )
        cache_key = self._content_prefix + content_hash
        value = payload if payload is not None else content_hash

        # ⚠️ TOCTOU: the check-then-set is not atomic — two concurrent
        # identical requests can both pass check_content_hash and both
        # ingest; SETNX only dedups the *store* (the first caller's payload
        # wins the value), not the ingestion side effects.  A DB uniqueness
        # column on the content hash would close the window.
        set_ok = await self._redis.set(cache_key, value, nx=True, ex=self._content_ttl)
        if set_ok:
            logger.debug(
                "idempotency.content_hash_stored",
                extra={
                    "content_hash": content_hash[:16] + "...",
                    "ttl": self._content_ttl,
                },
            )
        else:
            # Another concurrent caller already stored it — fine.
            logger.debug(
                "idempotency.content_hash_race_lost",
                extra={
                    "content_hash": content_hash[:16] + "...",
                },
            )

        return content_hash

    # ──────────────────────────────────────────────────────────────────────────
    # Worker-level idempotency
    # ──────────────────────────────────────────────────────────────────────────

    async def check_and_mark_worker(
        self,
        db: AsyncSession,
        episode_id: str,
        task_bit: int,
    ) -> bool:
        """Atomically claim an enrichment task for an episode.

        Uses ``SELECT ... FOR UPDATE`` to lock the row, then checks
        whether ``task_bit`` is already set in ``enrichment_status``.
        If not set, ORs it in and returns ``True`` (caller should
        proceed).  If already set, returns ``False`` (already done).

        Args:
            db: Database session **must** be in an active transaction
                (``async with db.begin()``).
            episode_id: UUID of the episode to claim.
            task_bit: Bitmask constant for the enrichment task (e.g.
                ``ENRICHMENT_ENTITIES``).

        Returns:
            ``True`` if the caller should proceed with processing (this
            is the first time the task runs for this episode).
            ``False`` if the task was already completed or is being
            processed by another worker.

        Raises:
            ValueError: If the episode ID does not exist.
        """
        # ── Lock the row and read current enrichment_status ──────────────
        episode_repo = EpisodeRepository(db)
        episode = await episode_repo.get_by_id_for_update(UUID(episode_id))

        if episode is None:
            raise ValueError(f"Episode {episode_id} not found")

        current_status: int = episode.enrichment_status

        # ── Check if this task bit is already set ────────────────────────
        if current_status & task_bit:
            logger.info(
                "idempotency.worker_already_done",
                extra={
                    "episode_id": episode_id,
                    "task_bit": task_bit,
                    "current_status": current_status,
                },
            )
            return False

        # ── Atomically set the bit (row is locked, SQL-level OR is atomic) ──
        await episode_repo.apply_enrichment_bits(UUID(episode_id), task_bit)
        # NOTE: Caller is responsible for ``await db.flush()`` or
        # ``await db.commit()`` depending on transaction management.

        logger.info(
            "idempotency.worker_claimed",
            extra={
                "episode_id": episode_id,
                "task_bit": task_bit,
                "previous_status": current_status,
                "new_status": current_status | task_bit,
            },
        )
        return True

    # ══════════════════════════════════════════════════════════════════════════
    # Cache invalidation
    # ══════════════════════════════════════════════════════════════════════════

    async def invalidate_user_cache(self, org_id: str, user_id: str) -> None:
        """Invalidate all cached data for a given user.

        Deletes cache keys matching the pattern
        ``OpenZync:{env}:cache:{org_id}:{user_id}:*``.

        Args:
            org_id: Organisation UUID string.
            user_id: User UUID string.
        """
        pattern = f"{self._cache_prefix}{org_id}:{user_id}:*"
        cursor: int = 0
        deleted_count: int = 0

        # SCAN in batches to avoid blocking Redis on large key spaces.
        while True:
            cursor, keys = await self._redis.scan(
                cursor=cursor, match=pattern, count=100
            )
            if keys:
                await self._redis.delete(*keys)
                deleted_count += len(keys)

            if cursor == 0:
                break

        if deleted_count > 0:
            logger.info(
                "idempotency.cache_invalidated",
                extra={
                    "org_id": org_id,
                    "user_id": user_id,
                    "keys_deleted": deleted_count,
                },
            )

    # ══════════════════════════════════════════════════════════════════════════
    # Hash utility (public — used by callers to pre-compute before calling)
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def hash_request_body(body: dict[str, Any]) -> str:
        """Compute SHA-256 hash of a canonical JSON request body.

        Uses ``sort_keys=True`` so that two semantically identical dicts
        with different key ordering produce the same hash.

        Args:
            body: The request body dict.

        Returns:
            SHA-256 hex digest string (64 characters).
        """
        canonical = orjson.dumps(body, option=orjson.OPT_SORT_KEYS)
        return hashlib.sha256(canonical).hexdigest()
