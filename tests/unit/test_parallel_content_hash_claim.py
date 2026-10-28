"""Parallel ingest: exactly one content-hash claim wins.

Two identical concurrent ingests race the content-hash claim. Exactly one
takes the single 202 path; the loser replays the winner's payload (job id)
instead of inserting a second time.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from services.idempotency_service import ContentHashClaim, IdempotencyService

pytestmark = pytest.mark.unit

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _mock_idempotency_service() -> AsyncMock:
    """Benign idempotency double — every ingest proceeds unless overridden."""
    from services.idempotency_service import IdempotencyResult, IdempotencyStatus

    idem = AsyncMock()
    idem.check_idempotency_key.return_value = IdempotencyResult(
        status=IdempotencyStatus.NEW
    )
    idem.check_content_hash.return_value = None
    idem.compute_content_hash = MagicMock(return_value="abc123")
    idem.claim_content_hash.return_value = ContentHashClaim(won=True, winner="")
    return idem


class TestParallelContentHashClaim:
    """Two identical concurrent ingests → one winner, one replay.

    The fake ``eval`` below implements the Lua script's atomic GET-or-SET
    with a real lock, standing in for Redis's indivisible script
    execution. What is under test is the service's interpretation:
    concurrent identical submissions cannot both win.
    """

    @pytest.mark.asyncio
    async def test_concurrent_identical_claims_single_winner(self) -> None:
        store: dict[str, str] = {}
        gate = asyncio.Lock()

        async def _atomic_eval(
            script: str, nkeys: int, key: str, payload: str, ttl: int
        ) -> list:
            async with gate:
                if key in store:
                    return [0, store[key]]
                store[key] = payload
                return [1, payload]

        redis = AsyncMock()
        redis.eval.side_effect = _atomic_eval
        service = IdempotencyService(redis=redis)
        messages = [{"role": "user", "content": "hello"}]

        results = await asyncio.gather(
            service.claim_content_hash(
                str(ORG_ID), str(USER_ID), "s1", messages, payload="job-A"
            ),
            service.claim_content_hash(
                str(ORG_ID), str(USER_ID), "s1", messages, payload="job-B"
            ),
        )

        winners = [r for r in results if r.won]
        losers = [r for r in results if not r.won]
        assert len(winners) == 1, "exactly one concurrent ingest proceeds"
        assert len(losers) == 1, "the loser must replay, never double-insert"
        # The loser replays the winner's payload (job_id), not its own.
        assert losers[0].winner == winners[0].winner
        assert losers[0].winner in ("job-A", "job-B")
