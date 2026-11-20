"""Idempotency-key collapse for ``llm_usage`` inserts.

GROUP 2 (idempotency half): two inserts with the same ``idempotency_key``
collapse to one row via Postgres ``ON CONFLICT DO NOTHING``. No live DB —
assertions run against the statements captured at the session boundary,
verifying the conflict clause and the carried key on every insert.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from core.llm import UsageRecord
from repositories.llm_usage_repository import LLMUsageRepository

pytestmark = pytest.mark.unit

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")


def _record(key: str) -> UsageRecord:
    return UsageRecord(
        user_id=ORG_ID,
        provider="openai",
        model="gpt-4o-mini",
        worker="enrich_episode",
        duration_ms=12,
        prompt_tokens=5,
        completion_tokens=7,
        idempotency_key=key,
    )


def _compiled(stmt: object) -> str:
    return str(
        stmt.compile(  # type: ignore[union-attr]
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


class TestIdempotencyCollapse:
    async def test_same_key_twice_issues_conflict_do_nothing(self) -> None:
        db = AsyncMock(spec=AsyncSession)
        repo = LLMUsageRepository(db)
        record = _record("dup-key")

        await repo.create(record)
        await repo.create(record)

        assert db.execute.await_count == 2
        for call in db.execute.await_args_list:
            compiled = _compiled(call.args[0]).upper()
            assert "ON CONFLICT" in compiled
            assert "DO NOTHING" in compiled
            assert "DUP-KEY" in compiled

    async def test_distinct_keys_produce_distinct_statements(self) -> None:
        db = AsyncMock(spec=AsyncSession)
        repo = LLMUsageRepository(db)

        await repo.create(_record("key-1"))
        await repo.create(_record("key-2"))

        assert db.execute.await_count == 2
        first = _compiled(db.execute.await_args_list[0].args[0])
        second = _compiled(db.execute.await_args_list[1].args[0])
        assert "key-1" in first
        assert "key-2" not in first
        assert "key-2" in second
        assert "key-1" not in second
