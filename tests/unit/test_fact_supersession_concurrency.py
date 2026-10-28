"""Fact supersession under concurrency: two writers, one active fact.

Two parallel conflicting writers are serialized (winner inserts, loser
re-scans and supersedes), so exactly one fact stays active.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from services.fact_invalidation_service import FactInvalidationService

pytestmark = pytest.mark.unit

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _row(**overrides) -> SimpleNamespace:
    return SimpleNamespace(
        id=overrides.get("id", uuid4()),
        subject=overrides.get("subject", "Alice"),
        predicate=overrides.get("predicate", "likes"),
        object=overrides.get("object", "hiking"),
        content=overrides.get("content", "Alice likes hiking"),
        subject_entity_id=None,
        object_entity_id=None,
        valid_to=None,
        invalid_at=None,
    )


class TestSupersessionConcurrency:
    """Two parallel conflicting writers leave exactly one active fact.

    The fake advisory lock is a real ``asyncio.Lock`` held across each
    writer's scan+insert (released by the insert, mirroring the PG xact
    lock the winner's commit releases): the loser blocks, then its
    re-scan sees the winner's row and supersedes it.
    """

    @pytest.mark.asyncio
    async def test_two_parallel_writers_one_active_fact(self) -> None:
        table: list[SimpleNamespace] = []
        gate = asyncio.Lock()
        mock_db = AsyncMock(spec=AsyncSession)
        mock_db.sync_session = Session()
        repo = AsyncMock()
        repo.find_retracted_by_match_keys.return_value = []

        async def _lock(keys: list[str]) -> None:
            await gate.acquire()

        async def _scan(**kwargs) -> list[SimpleNamespace]:
            await asyncio.sleep(0)  # widen the race window
            return [r for r in table if r.valid_to is None]

        async def _insert(*, facts: list[dict], **kwargs) -> list[SimpleNamespace]:
            rows = [_row(**f) for f in facts]
            table.extend(rows)
            gate.release()
            return rows

        async def _close(fact_id: UUID, now: datetime) -> None:
            for r in table:
                if r.id == fact_id:
                    r.valid_to = now

        repo.lock_conflict_identities.side_effect = _lock
        repo.find_conflicting_active_for_update.side_effect = _scan
        repo.batch_create.side_effect = _insert
        repo.set_valid_to.side_effect = _close
        service = FactInvalidationService(db=mock_db, fact_repo=repo)

        def _payload(content: str) -> dict:
            return {
                "subject": "Alice",
                "predicate": "likes",
                "object": "hiking",
                "content": content,
            }

        results = await asyncio.gather(
            service.ingest_with_supersession(
                org_id=ORG_ID,
                project_id=PROJECT_ID,
                user_id=USER_ID,
                facts=[_payload("Alice likes hiking v1")],
                now=NOW,
            ),
            service.ingest_with_supersession(
                org_id=ORG_ID,
                project_id=PROJECT_ID,
                user_id=USER_ID,
                facts=[_payload("Alice likes hiking v2")],
                now=NOW,
            ),
        )

        active = [r for r in table if r.valid_to is None]
        assert len(table) == 2, "both writers insert exactly once"
        assert len(active) == 1, "exactly one fact stays active"
        assert sorted(r.superseded_count for r in results) == [0, 1]
