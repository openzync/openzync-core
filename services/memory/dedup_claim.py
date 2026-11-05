"""Dedup claim helpers — atomic content-hash claim (TOCTOU-safe)."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from core.exceptions import ConflictError
from schemas.memory import IngestMemoryResponse

logger = logging.getLogger(__name__)


def _encode_claim_payload(job_id: UUID, episode_count: int) -> str:
    """Encode the dedup claim payload as ``"{job_id}:{episode_count}"``.

    The count travels with the claim so a replay returns the winner's
    ``episode_count`` instead of recomputing it from the loser's request.
    """
    return f"{job_id}:{episode_count}"


def _decode_claim_payload(
    payload: str, *, fallback_count: int
) -> tuple[str, int]:
    """Split a claim payload into ``(job_id, episode_count)``.

    Legacy payloads stored a bare ``job_id`` (no ``:count`` suffix) —
    those fall back to ``fallback_count``.

    Args:
        payload: The stored claim value (winner's payload on replay).
        fallback_count: Count to use when the payload carries none.

    Returns:
        Tuple of ``(job_id, episode_count)``.
    """
    job_id, sep, count_str = payload.partition(":")
    if not sep:
        return payload, fallback_count
    try:
        return job_id, int(count_str)
    except ValueError:
        return job_id, fallback_count


class DedupClaimMixin:
    """Request idempotency + atomic content-hash claim for ingest."""

    _db: Any
    _idem: Any
    _dedup_repo: Any

    async def _check_idempotency_key(
        self,
        idempotency_key: str,
        body_hash: str,
        org_id: UUID,
        project_id: UUID,
    ) -> IngestMemoryResponse | None:
        """Return the replay response for a duplicate key, if any.

        Args:
            idempotency_key: Client-supplied idempotency key.
            body_hash: SHA-256 of the canonical request body.
            org_id: Tenant scope (log context).
            project_id: Project scope (log context).

        Returns:
            The stored replay response, or ``None`` to proceed.

        Raises:
            ConflictError: If the key was used with a different body.
        """
        from services.idempotency_service import IdempotencyStatus

        result = await self._idem.check_idempotency_key(
            idempotency_key, body_hash, str(org_id)
        )
        if (
            result.status == IdempotencyStatus.REPLAY
            and result.response_data is not None
        ):
            logger.info(
                "memory.idempotency_replay",
                extra={
                    "idempotency_key": idempotency_key[:16] + "...",
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                },
            )
            return IngestMemoryResponse(**result.response_data)
        if result.status == IdempotencyStatus.CONFLICT:
            raise ConflictError(
                "Idempotency-Key already used with a different request body"
            )
        return None

    async def _claim_batch(
        self,
        org_id: UUID,
        created_by: UUID,
        session_id: UUID,
        msgs: list[dict[str, Any]],
        project_id: UUID,
    ) -> tuple[UUID, IngestMemoryResponse | None]:
        """Atomically claim the batch; replay the winner on loss.

        Redis Lua claim first, then the ``ingest_dedup`` UNIQUE backstop
        (covers Redis eviction/failover races). A lost claim returns the
        winner's response instead of inserting.

        Args:
            org_id: Tenant scope.
            created_by: Attributing user.
            session_id: Target session.
            msgs: Canonical message dicts for hashing.
            project_id: Project scope (log context + dedup row).

        Returns:
            Tuple of ``(job_id, replay)`` — ``replay`` is non-``None``
            when this attempt lost and must return the winner's response.
        """
        content_hash = self._idem.compute_content_hash(
            str(org_id), str(created_by), str(session_id), msgs
        )
        try:
            _peek = await self._idem.check_content_hash(
                str(org_id), str(created_by), str(session_id), msgs
            )
        except Exception:
            _peek = None
            logger.warning(
                "memory.content_dedup_peek_failed",
                extra={
                    "content_hash": content_hash[:16] + "...",
                    "project_id": str(project_id),
                },
            )
        if _peek is not None:
            logger.info(
                "memory.content_dedup_peek_hit",
                extra={
                    "content_hash": content_hash[:16] + "...",
                    "peek_job_id": _peek,
                    "project_id": str(project_id),
                },
            )
        # note: intentionally NO early return here — every caller falls
        # through to the atomic claim below, the sole arbiter.
        job_id = uuid4()
        claim = await self._idem.claim_content_hash(
            str(org_id),
            str(created_by),
            str(session_id),
            msgs,
            payload=_encode_claim_payload(job_id, len(msgs)),
        )
        if not claim.won:
            logger.info(
                "memory.content_dedup_hit",
                extra={
                    "content_hash": content_hash[:16] + "...",
                    "existing_job_id": claim.winner,
                    "project_id": str(project_id),
                },
            )
            winner_job_id, winner_count = _decode_claim_payload(
                claim.winner, fallback_count=len(msgs)
            )
            return job_id, IngestMemoryResponse(
                job_id=winner_job_id,
                episode_count=winner_count,
                status="accepted",
                message="Content already ingested; returning existing job_id",
            )

        async def _loser_response(
            prior_job_id: UUID | None, via: str
        ) -> IngestMemoryResponse:
            logger.info(
                "memory.content_dedup_hit",
                extra={
                    "content_hash": content_hash,
                    "existing_job_id": str(prior_job_id) if prior_job_id else None,
                    "project_id": str(project_id),
                    "via": via,
                },
            )
            await self._idem.release_content_hash(
                str(org_id), str(created_by), str(session_id), msgs
            )
            return IngestMemoryResponse(
                job_id=str(prior_job_id) if prior_job_id else None,
                episode_count=len(msgs),
                status="accepted",
                message="Content already ingested; returning existing job_id",
            )

        try:
            _claimed = await self._dedup_repo.insert_or_none(
                project_id=project_id,
                session_id=session_id,
                content_hash=content_hash,
                job_id=job_id,
            )
        except IntegrityError:
            await self._db.rollback()
            prior_job_id = await self._dedup_repo.get_job_id(
                project_id=project_id,
                session_id=session_id,
                content_hash=content_hash,
            )
            # Our Redis claim holds this attempt's job_id but the DB
            # winner stands — release so future replays resolve the true
            # winner via the DB backstop instead of our dead claim.
            return job_id, await _loser_response(prior_job_id, "integrity_error")
        if not _claimed:
            prior_job_id = await self._dedup_repo.get_job_id(
                project_id=project_id,
                session_id=session_id,
                content_hash=content_hash,
            )
            return job_id, await _loser_response(prior_job_id, "row_exists")
        # Won both claims — the caller holds the claim from here on; any
        # failure below must release it so a retry re-claims.
        return job_id, None
