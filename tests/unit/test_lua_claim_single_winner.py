"""Lua claim single-winner — atomic dedup arbiter (no I/O, mocked Redis)."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from services.idempotency_service import ContentHashClaim, IdempotencyService


def _service_with_script(winner_payload: str, won_flag: int) -> IdempotencyService:
    redis = AsyncMock()
    redis.eval.return_value = [won_flag, winner_payload.encode()]
    return IdempotencyService(redis)


@pytest.mark.unit
class TestLuaClaimSingleWinner:
    @pytest.mark.asyncio
    async def test_first_claim_wins_second_replays(self) -> None:
        """Concurrent identical batches: exactly one winner, loser replays."""
        job_a = str(uuid4())
        svc_a = _service_with_script(job_a, 1)
        claim_a = await svc_a.claim_content_hash(
            "org", "user", "sess", [{"role": "user", "content": "hi"}],
            payload=job_a,
        )
        assert claim_a.won is True

        svc_b = _service_with_script(job_a, 0)
        claim_b = await svc_b.claim_content_hash(
            "org", "user", "sess", [{"role": "user", "content": "hi"}],
            payload=str(uuid4()),
        )
        assert claim_b.won is False
        assert isinstance(claim_b, ContentHashClaim)
        assert claim_b.winner == job_a
