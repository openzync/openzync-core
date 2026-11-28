"""Idempotency & Deduplication service — three layers of protection.

Layers
------
1. **HTTP-level** (``Idempotency-Key`` header):
   Prevents duplicate processing when a client retries the same request.
   Backed by Redis with 48h TTL.  Returns cached response on replay,
   raises conflict on key reuse with different payload.

2. **Content-level** (SHA-256 content hash):
   Prevents the same ``(user_id, user_id, session_id, messages)``
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
    result = await service.check_idempotency_key(key, body_hash, user_id)
    if result.status == IdempotencyStatus.NEW:
        response_data = await process_fn()
        await service.store_idempotency_key(key, body_hash, response_data, user_id)

    # Content-level
    # Atomic claim (preferred — single Lua GET-or-SET, no TOCTOU):
    claim = await service.claim_content_hash(
        user_id, session_id, messages, payload=job_id
    )
    if not claim.won:
        return claim.winner  # duplicate — replay winner payload (e.g. job_id)
    # ... proceed with ingestion ...
    # Release on failure so a retry can claim fresh (never a stale replay):
    try:
        ...  # proceed with ingestion
    except Exception:
        await service.release_content_hash(user_id, session_id, messages)
        raise
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

from core.config import get_settings
from workers.tasks.base import (  # noqa: F401  — re-export
    ENRICHMENT_ALL,
    ENRICHMENT_EMBEDDING,
    ENRICHMENT_ENTITIES,
    ENRICHMENT_ENTITY_LINKS,
    ENRICHMENT_FACTS,
)

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
        response_data: Cached response body when ``status == REPLAY`` —
            also populated on ``CONFLICT`` so callers can surface the
            prior ``job_id`` in the 409 body for reconciliation.
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


def _coerce_response_data(value: Any) -> dict[str, Any] | None:
    """Normalize a cached ``response_body`` to a dict.

    Older cache entries (or foreign writers) may hold a JSON string
    instead of a dict — a raw ``str`` would fail the
    ``isinstance(..., dict)`` gate downstream and silently drop the
    ``job_id`` from 409 bodies. Non-dict, non-JSON values yield ``None``.

    Args:
        value: The raw cached ``response_body`` value.

    Returns:
        The value as a dict, or ``None`` when it cannot be coerced.
    """
    if value is None or isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes)):
        try:
            parsed: Any = orjson.loads(value)
        except (orjson.JSONDecodeError, TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


class ContentHashClaim:
    """Outcome of an atomic content-hash claim.

    Attributes:
        won: ``True`` when this caller won the claim and must proceed
            with ingestion; ``False`` on a duplicate.
        winner: The winning payload — the winner's ``job_id`` when
            ``won`` is ``True``, the earlier winner's payload on replay.
    """

    __slots__ = ("won", "winner")

    def __init__(self, won: bool, winner: str) -> None:
        self.won = won
        self.winner = winner

    def __repr__(self) -> str:
        return f"ContentHashClaim(won={self.won}, winner={self.winner!r})"


# Single atomic claim: GET → replay the winner's payload when present,
# else SET NX EX the caller's payload.  Runs as one Lua script so the
# check-and-set is indivisible — concurrent identical submissions cannot
# both win, closing the TOCTOU window of check-then-store.
_CLAIM_CONTENT_HASH_LUA: str = """
local existing = redis.call('GET', KEYS[1])
if existing then
  return {0, existing}
end
local ok = redis.call('SET', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2])
if ok then
  return {1, ARGV[1]}
else
  return {0, redis.call('GET', KEYS[1])}
end
"""


# ═══════════════════════════════════════════════════════════════════════════════
# Worker-level bitmask constants
# ═══════════════════════════════════════════════════════════════════════════════
# These constants represent each enrichment task's bit position in the
# ``episodes.enrichment_status`` integer column.  Combine with bitwise OR (``|``)
# to mark multiple tasks as completed.
#
# Re-exported from workers.tasks.base (imported above) — the single source of
# truth.  This module previously redefined the bitmask and got it WRONG: it
# OR'd only bits 0-3, yielding ENRICHMENT_ALL = 15 instead of 63, so any caller
# that treated it as "fully enriched" would have accepted a half-done episode.


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
        self, key: str, body_hash: str, user_id: str | UUID
    ) -> IdempotencyResult:
        """Check an ``Idempotency-Key`` and return the appropriate action.

        Keys are tenant-namespaced — the cache key is
        ``OpenZync:{env}:idempotency:{user_id}:{key}`` — so the same key
        used by two different users never collides.

        Args:
            key: The ``Idempotency-Key`` header value (max 255 chars).
            body_hash: SHA-256 hex digest of the canonical request body.
            user_id: The tenant (user) UUID or string. Normalized
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

        cache_key = f"{self._idem_prefix}{user_id}:{key}"

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
            # Same key, different body — this is a conflict.
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
                # Carry the prior response so callers can surface its
                # job_id in the 409 body for client reconciliation.
                # Coerced: older entries may hold a JSON string.
                response_data=_coerce_response_data(
                    entry.get("response_body")
                ),
                message=(
                    "Idempotency-Key already used with a different request body. "
                    "Each Idempotency-Key must uniquely identify a single request."
                ),
            )

        # Same key, same body — safe replay. Coerced for the same reason.
        response_data: dict[str, Any] | None = _coerce_response_data(
            entry.get("response_body")
        )
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
        user_id: str | UUID,
    ) -> None:
        """Persist a successful response for future idempotency replay.

        The cache key is tenant-namespaced
        (``OpenZync:{env}:idempotency:{user_id}:{key}``) so replays cannot
        cross user boundaries.

        Args:
            key: The ``Idempotency-Key`` header value.
            body_hash: SHA-256 hex digest of the canonical request body.
            response_data: The response dict to cache for replay.
            user_id: The tenant (user) UUID or string. Normalized
                to ``str`` for the cache key.
        """
        cache_key = f"{self._idem_prefix}{user_id}:{key}"
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
        user_id: str,
        session_id: str,
        messages: list[dict[str, Any]],
    ) -> str:
        """Compute a deterministic SHA-256 hash for content dedup.

        The hash is based on ``(user_id, user_id, session_id, role,
        content)``.  ``metadata`` and ``blobs`` are included in each
        message's canonical form when those keys are present in the
        message dict, so identical payloads that differ only in absence
        vs presence of metadata/blobs do not collide.

        Args:
            user_id: User UUID string.
            user_id: User UUID string.
            session_id: Session UUID or external ID string.
            messages: List of message dicts, each containing at minimum
                ``role`` and ``content`` keys.

        Returns:
            SHA-256 hex digest string (64 characters).
        """
        canonical = orjson.dumps(
            {
                "user_id": user_id,
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
        user_id: str,
        session_id: str,
        messages: list[dict[str, Any]],
    ) -> str | None:
        """Peek at a content hash (metrics/log best-effort ONLY).

        Computes the SHA-256 hash and looks it up in Redis for
        observability (dedup-peek hit counters, debug logs). The return
        value must NEVER gate ingestion — a check-then-act gate here is
        a TOCTOU race: two concurrent identical submissions can both
        read ``None`` and both ingest. The sole ingest arbiter is
        :meth:`claim_content_hash` (atomic Lua GET-or-SET) with the
        ``ingest_dedup`` UNIQUE claim as DB backstop.

        Args:
            user_id: User UUID string.
            user_id: User UUID string.
            session_id: Session UUID string.
            messages: List of message dicts.

        Returns:
            The stored payload (e.g. the original ``job_id``) if this
            content was already ingested, ``None`` if it is new — hint
            only, not a decision.
        """
        content_hash = self.compute_content_hash(
            user_id, session_id, messages
        )
        cache_key = self._content_prefix + content_hash

        cached: str | None = await self._redis.get(cache_key)
        if cached is not None:
            logger.info(
                "idempotency.content_dedup_hit",
                extra={
                    "user_id": user_id,
                    "session_id": session_id,
                    "message_count": len(messages),
                },
            )
            return cached

        return None

    async def claim_content_hash(
        self,
        user_id: str,
        session_id: str,
        messages: list[dict[str, Any]],
        *,
        payload: str,
    ) -> ContentHashClaim:
        """Atomically claim a content hash via a single Lua script.

        ``GET`` replays the winner's payload when the hash was already
        claimed; otherwise ``SET NX EX`` stores the caller's payload and
        reports a win.  The script is indivisible, so concurrent identical
        submissions cannot both win — exactly one caller proceeds to
        ingest, the losers replay the winner's payload (e.g. ``job_id``)
        without inserting anything.

        Args:
            user_id: User UUID string.
            user_id: User UUID string.
            session_id: Session UUID string.
            messages: List of message dicts.
            payload: Value stored on a win (the winner's ``job_id``).

        Returns:
            A :class:`ContentHashClaim` — ``won=True`` means proceed with
            ingestion; ``won=False`` means replay ``winner`` instead.
        """
        content_hash = self.compute_content_hash(user_id, session_id, messages)
        cache_key = self._content_prefix + content_hash

        # ``eval`` is typed as returning ``str`` in redis-py's stubs but
        # returns the script's reply (a two-element list here) at runtime.
        eval_fn: Any = self._redis.eval
        raw: Any = await eval_fn(
            _CLAIM_CONTENT_HASH_LUA, 1, cache_key, payload, self._content_ttl
        )
        won_flag, winner = raw[0], raw[1]
        won = won_flag == 1 or won_flag == b"1" or won_flag == "1"
        if isinstance(winner, bytes):
            winner = winner.decode("utf-8")

        if won:
            logger.debug(
                "idempotency.content_hash_claim_won",
                extra={"content_hash": content_hash[:16] + "..."},
            )
        else:
            logger.info(
                "idempotency.content_hash_claim_replay",
                extra={
                    "user_id": user_id,
                    "session_id": session_id,
                    "message_count": len(messages),
                },
            )
        return ContentHashClaim(won=won, winner=winner)

    async def release_content_hash(
        self,
        user_id: str,
        session_id: str,
        messages: list[dict[str, Any]],
    ) -> None:
        """Release a previously won content-hash claim (failure path only).

        Deletes the Redis claim key so a retry of the same content can
        claim fresh instead of replaying a ``job_id`` whose ingest never
        completed. The ``ingest_dedup`` DB row remains the arbiter: if the
        failed attempt persisted it, the retry replays the winner via the
        DB backstop; if it rolled back, the retry proceeds cleanly.

        Fail-open: Redis errors are logged and swallowed — a lost release
        only risks a stale replay, never a failed request. The claim path
        (:meth:`claim_content_hash`) stays fail-closed.

        Args:
            user_id: User UUID string.
            user_id: User UUID string.
            session_id: Session UUID string.
            messages: List of message dicts (recomputes the claim key).
        """
        content_hash = self.compute_content_hash(
            user_id, session_id, messages
        )
        try:
            await self._redis.delete(self._content_prefix + content_hash)
        except Exception:
            logger.warning(
                "idempotency.content_hash_release_failed",
                extra={"content_hash": content_hash[:16] + "..."},
                exc_info=True,
            )

    # ──────────────────────────────────────────────────────────────────────────
    # Worker-level idempotency
    # ──────────────────────────────────────────────────────────────────────────

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
