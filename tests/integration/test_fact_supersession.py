"""Integration tests for fact supersession + temporal as-of (Phase 2).

Runs against the testcontainers PostgreSQL + Redis stack.  Verifies the
supersession state machine end-to-end at the service/repository layer
with real transactions, FOR UPDATE serialization, advisory-lock
serialization of true races, and the effective-at predicate.

Temporal determinism: both write paths (``batch_create`` and
``batch_create_or_skip``) honor the row's ``valid_from``, so
supersession produces exactly adjacent ``[a, now) + [now, inf)``
ranges.

Scenarios covered (architect's 14 adapted to the observed contract):
    1. Same-episode re-extraction with different content → old truncated
       at ``now``; as-of before shows old, after shows new.
    2. Cross-episode same SPO → supersedes (was silent coexistence).
    3. Idempotent re-run of the same SPO + content → zero new rows, no
       truncation.
    5. Concurrent supersession of the same SPO → advisory xact lock
       serializes even a true race; chained non-overlapping ranges, no
       exclusion-constraint violation.
    6. Adjacent ``[a, now)`` + ``[now, inf)`` ranges never trip the
       ``uq_facts_temporal_excl`` exclusion constraint.
    7. Insert failure after truncate → rollback leaves the old fact
       intact.
    8. As-of retrieval across a supersession boundary (repo level).
    9. Vector & BM25 retrieval exclude superseded facts for default
       (now) queries.
    13. Supersession never touches the source episode's enrichment bit
        (no reconcile_enrichment ping-pong).
    14. The conflict scan respects ``organization_id`` — same SPO in
        another org does not supersede.
    Worker path: ``batch_create_or_skip`` supersedes correctly at real
       now.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.fact_repository import FactRepository
from services.cache_service import CacheService
from services.fact_invalidation_service import FactInvalidationService
from services.hybrid_retriever import HybridRetriever

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
T1 = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)
T2 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


# ── Seeding helpers ──────────────────────────────────────────────────────────


def _new_uuid() -> UUID:
    return uuid4()


async def _seed_user(db: AsyncSession, user_id: UUID, org_id: UUID = ORG_ID) -> None:
    await db.execute(
        sa_text(
            "INSERT INTO users (id, organization_id, external_id, name, "
            "role, is_active) "
            "VALUES (:uid, :org_id, :eid, :name, 'member', true) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {
            "uid": user_id,
            "org_id": org_id,
            "eid": f"ss-user-{user_id}",
            "name": "Supersession Test User",
        },
    )


async def _seed_org(db: AsyncSession, org_id: UUID) -> None:
    await db.execute(
        sa_text(
            "INSERT INTO organizations (id, name, plan) VALUES (:oid, :name, 'free') "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"oid": org_id, "name": f"org-{org_id}"},
    )


async def _seed_project(
    db: AsyncSession, project_id: UUID, org_id: UUID = ORG_ID
) -> None:
    await db.execute(
        sa_text(
            "INSERT INTO projects (id, organization_id, name) "
            "VALUES (:pid, :oid, :name) ON CONFLICT (id) DO NOTHING"
        ),
        {"pid": project_id, "oid": org_id, "name": f"proj-{project_id}"},
    )


async def _seed_episode(
    db: AsyncSession,
    episode_id: UUID,
    project_id: UUID,
    user_id: UUID,
    org_id: UUID = ORG_ID,
) -> None:
    session_id = uuid4()
    await db.execute(
        sa_text(
            "INSERT INTO sessions (id, organization_id, project_id, user_id, "
            "external_id, is_active) "
            "VALUES (:sid, :org_id, :proj_id, :uid, :eid, true) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {
            "sid": session_id,
            "org_id": org_id,
            "proj_id": project_id,
            "uid": user_id,
            "eid": f"ss-session-{episode_id}",
        },
    )
    await db.execute(
        sa_text(
            "INSERT INTO episodes (id, organization_id, project_id, session_id, "
            "user_id, role, content, token_count, sequence_number, enrichment_status) "
            "VALUES (:eid, :org_id, :proj_id, :sid, :uid, 'user', 'test', 0, 1, 0) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {
            "eid": episode_id,
            "org_id": org_id,
            "proj_id": project_id,
            "sid": session_id,
            "uid": user_id,
        },
    )


def _triple(
    subject: str = "Alice",
    predicate: str = "likes",
    obj: str = "hiking",
    content: str | None = None,
) -> dict[str, Any]:
    return {
        "subject": subject,
        "predicate": predicate,
        "object": obj,
        "content": content or f"{subject} {predicate} {obj}",
        "confidence": 0.95,
    }


async def _ingest(
    engine,
    *,
    facts: list[dict[str, Any]],
    user_id: UUID,
    project_id: UUID = PROJECT_ID,
    org_id: UUID = ORG_ID,
    source_episode_id: UUID | None = None,
    insert_mode: str = "batch_create",
    now: datetime | None = None,
) -> Any:
    """Run one supersession ingestion in its own session and commit."""
    db = AsyncSession(engine, expire_on_commit=False)
    try:
        repo = FactRepository(db)
        service = FactInvalidationService(db=db, fact_repo=repo)
        result = await service.ingest_with_supersession(
            org_id=org_id,
            project_id=project_id,
            user_id=user_id,
            facts=facts,
            source_episode_id=source_episode_id,
            insert_mode=insert_mode,  # type: ignore[arg-type]
            now=now,
        )
        await db.commit()
        return result
    finally:
        await db.close()


async def _facts_at(
    engine, timestamp: datetime, project_id: UUID = PROJECT_ID, org_id: UUID = ORG_ID
) -> list[Any]:
    db = AsyncSession(engine, expire_on_commit=False)
    try:
        repo = FactRepository(db)
        return await repo.get_facts_at_time(
            project_id, timestamp, organization_id=org_id, limit=200
        )
    finally:
        await db.close()


async def _spo_rows(engine, subject: str, project_id: UUID = PROJECT_ID) -> list[Any]:
    db = AsyncSession(engine, expire_on_commit=False)
    try:
        result = await db.execute(
            sa_text(
                "SELECT content, valid_from, valid_to FROM facts "
                "WHERE project_id = :pid AND subject = :subj ORDER BY valid_from"
            ),
            {"pid": project_id, "subj": subject},
        )
        return result.fetchall()
    finally:
        await db.close()


class TestSameEpisodeReExtraction:
    """Scenarios 1 + 6 — supersession truncates, as-of sees both eras."""

    async def test_supersedes_and_as_of_retrieval(self, engine, db_session) -> None:
        """Old fact truncated at ``now``; as-of before/after sees old/new."""
        user_id = _new_uuid()
        episode_id = _new_uuid()
        async with AsyncSession(engine) as db:
            await _seed_user(db, user_id)
            await _seed_episode(db, episode_id, PROJECT_ID, user_id)
            await db.commit()

        r1 = await _ingest(
            engine,
            facts=[_triple(content="Alice likes hiking")],
            user_id=user_id,
            source_episode_id=episode_id,
            now=T0,
        )
        assert r1.inserted_count == 1
        assert r1.superseded_count == 0

        r2 = await _ingest(
            engine,
            facts=[_triple(content="Alice absolutely loves hiking")],
            user_id=user_id,
            source_episode_id=episode_id,
            now=T1,
        )
        assert r2.inserted_count == 1
        assert r2.superseded_count == 1

        # ── Temporal as-of across the boundary ─────────────────────────
        before = await _facts_at(engine, T0 + timedelta(minutes=1))
        assert [f.content for f in before] == ["Alice likes hiking"], (
            "As-of inside the original validity window must show the OLD content"
        )

        after = await _facts_at(engine, T1 + timedelta(minutes=1))
        assert [f.content for f in after] == ["Alice absolutely loves hiking"], (
            "As-of after supersession must show the NEW content only"
        )

        # The old fact is closed at exactly T1 — not hard-retracted.
        assert before[0].valid_to == T1
        assert before[0].invalid_at is None

    async def test_adjacent_ranges_no_exclusion_violation(
        self, engine, db_session
    ) -> None:
        """Scenario 6 — chained [a, now) ranges never trip the GiST constraint."""
        user_id = _new_uuid()
        episode_id = _new_uuid()
        async with AsyncSession(engine) as db:
            await _seed_user(db, user_id)
            await _seed_episode(db, episode_id, PROJECT_ID, user_id)
            await db.commit()

        instants = [T0, T1, T2]
        for instant, content in zip(
            instants,
            ("v1 cobalt output", "v2 cobalt output", "v3 cobalt output"),
            strict=True,
        ):
            result = await _ingest(
                engine,
                facts=[
                    _triple(
                        subject="Mine",
                        predicate="produces",
                        obj="cobalt",
                        content=content,
                    )
                ],
                user_id=user_id,
                source_episode_id=episode_id,
                now=instant,
            )
            assert result.inserted_count == 1
            assert result.superseded_count == (1 if instant != T0 else 0)

        rows = await _spo_rows(engine, "Mine")
        assert len(rows) == 3
        for i in range(2):
            assert rows[i].valid_to == rows[i + 1].valid_from, (
                "Supersession must produce exactly adjacent ranges "
                "[a, now) + [now, inf) — no gap, no overlap"
            )
        assert rows[2].valid_to is None
        assert rows[2].content == "v3 cobalt output"


class TestCrossEpisodeSupersession:
    """Scenario 2 — cross-episode same SPO supersedes instead of coexisting."""

    async def test_cross_episode_supersedes(self, engine, db_session) -> None:
        user_id = _new_uuid()
        ep1, ep2 = _new_uuid(), _new_uuid()
        async with AsyncSession(engine) as db:
            await _seed_user(db, user_id)
            await _seed_episode(db, ep1, PROJECT_ID, user_id)
            await _seed_episode(db, ep2, PROJECT_ID, user_id)
            await db.commit()

        r1 = await _ingest(
            engine,
            facts=[
                _triple(
                    subject="Acme",
                    predicate="hq_in",
                    obj="NYC",
                    content="Acme HQ in NYC",
                )
            ],
            user_id=user_id,
            source_episode_id=ep1,
            now=T0,
        )
        assert r1.superseded_count == 0

        r2 = await _ingest(
            engine,
            facts=[
                _triple(
                    subject="Acme",
                    predicate="hq_in",
                    obj="NYC",
                    content="Acme HQ moved to NYC",
                )
            ],
            user_id=user_id,
            source_episode_id=ep2,
            now=T1,
        )
        assert r2.superseded_count == 1, (
            "Cross-episode same SPO must supersede — not silently coexist"
        )

        current = await _facts_at(engine, T1 + timedelta(minutes=1))
        assert [f.content for f in current] == ["Acme HQ moved to NYC"]


class TestIdempotentReRun:
    """Scenario 3 — identical SPO + content is a no-op."""

    async def test_identical_re_run_no_new_rows_no_truncation(
        self, engine, db_session
    ) -> None:
        user_id = _new_uuid()
        async with AsyncSession(engine) as db:
            await _seed_user(db, user_id)
            await db.commit()

        r1 = await _ingest(
            engine,
            facts=[_triple(content="Alice likes hiking")],
            user_id=user_id,
            now=T0,
        )
        assert r1.inserted_count == 1

        r2 = await _ingest(
            engine,
            facts=[_triple(content="Alice likes hiking")],
            user_id=user_id,
            now=T1,
        )
        assert r2.inserted_count == 0
        assert r2.superseded_count == 0
        assert r2.skipped_count == 1

        current = await _facts_at(engine, T1 + timedelta(minutes=1))
        assert len(current) == 1
        assert current[0].valid_to is None, "Identical re-run must not truncate"


class TestConcurrentSupersession:
    """Scenario 5 — advisory-lock serialization, observed on this stack.

    The FOR UPDATE lock serializes supersession when the conflicting row
    already exists and is committed (the common ARQ-retry / sequential-
    write case): the second writer blocks on the row lock, re-reads, and
    supersedes the first writer's row.

    A **true race** (both writers in flight before either commits) is
    serialized by the per-identity advisory xact lock taken before the
    conflict scan: the loser blocks on the lock, and its re-scan (READ
    COMMITTED re-snapshots per statement) sees the winner's committed
    row → exactly one supersession, one open-ended fact.  Asserted below
    for both the API path (no source episode) and the worker path
    (shared episode).
    """

    async def test_serialized_supersession_after_prior_write(
        self, engine, db_session
    ) -> None:
        """Second write starts after the first commits → chained ranges."""
        user_id = _new_uuid()
        episode_id = _new_uuid()
        async with AsyncSession(engine) as db:
            await _seed_user(db, user_id)
            await _seed_episode(db, episode_id, PROJECT_ID, user_id)
            await db.commit()

        seed = await _ingest(
            engine,
            facts=[
                _triple(
                    subject="Stock", predicate="price_of", obj="OZ", content="OZ at $10"
                )
            ],
            user_id=user_id,
            source_episode_id=episode_id,
            now=T0,
        )
        assert seed.inserted_count == 1

        # Sequential writers — each sees the previous committed row and
        # supersedes it; FOR UPDATE serializes without contention.
        r1 = await _ingest(
            engine,
            facts=[
                _triple(
                    subject="Stock", predicate="price_of", obj="OZ", content="OZ at $11"
                )
            ],
            user_id=user_id,
            source_episode_id=episode_id,
            now=T1,
        )
        r2 = await _ingest(
            engine,
            facts=[
                _triple(
                    subject="Stock", predicate="price_of", obj="OZ", content="OZ at $12"
                )
            ],
            user_id=user_id,
            source_episode_id=episode_id,
            now=T2,
        )
        assert r1.superseded_count == 1
        assert r2.superseded_count == 1

        rows = await _spo_rows(engine, "Stock")
        assert len(rows) == 3
        for i in range(2):
            assert rows[i].valid_to == rows[i + 1].valid_from, (
                "Sequential supersessions must produce exactly chained ranges"
            )
        assert rows[2].valid_to is None
        assert rows[2].content == "OZ at $12"

    async def test_true_race_api_path_serialized_by_advisory_lock(
        self, engine, db_session
    ) -> None:
        """API path, true race → the advisory lock prevents coexistence.

        FOR UPDATE cannot lock a row that does not exist yet, so the
        loser would previously scan-empty and insert alongside the
        winner (silent coexistence, superseded_count 0/0).  The
        per-identity advisory xact lock closes that gap: the loser
        blocks, re-scans after the winner commits, and supersedes it.
        """
        user_id = _new_uuid()
        async with AsyncSession(engine) as db:
            await _seed_user(db, user_id)
            await db.commit()

        async def _race(content: str) -> Any:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                service = FactInvalidationService(db=db, fact_repo=FactRepository(db))
                result = await service.ingest_with_supersession(
                    org_id=ORG_ID,
                    project_id=PROJECT_ID,
                    user_id=user_id,
                    facts=[
                        _triple(
                            subject="Race",
                            predicate="state",
                            obj="flag",
                            content=content,
                        )
                    ],
                    now=T1,
                )
                await db.commit()
                return result

        results = await asyncio.gather(_race("flag red"), _race("flag green"))
        assert sum(r.superseded_count for r in results) == 1, (
            "Advisory lock must serialize the race — exactly one supersession, "
            f"got {[r.superseded_count for r in results]}"
        )

        rows = await _spo_rows(engine, "Race")
        assert len(rows) == 2
        assert sum(r.valid_to is None for r in rows) == 1, (
            "Exactly ONE open-ended fact must remain — no silent coexistence"
        )

    async def test_true_race_worker_path_serialized_by_advisory_lock(
        self, engine, db_session
    ) -> None:
        """Worker path, true race → the advisory lock, not the constraint.

        Previously the shared-episode GiST exclusion constraint rejected
        the loser loudly (and the ARQ retry converged).  The advisory
        lock now serializes the pair before any insert, so the loser
        supersedes the winner instead of tripping the constraint.
        """
        user_id = _new_uuid()
        episode_id = _new_uuid()
        async with AsyncSession(engine) as db:
            await _seed_user(db, user_id)
            await _seed_episode(db, episode_id, PROJECT_ID, user_id)
            await db.commit()

        async def _race(content: str) -> Any:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                service = FactInvalidationService(db=db, fact_repo=FactRepository(db))
                result = await service.ingest_with_supersession(
                    org_id=ORG_ID,
                    project_id=PROJECT_ID,
                    user_id=user_id,
                    facts=[
                        _triple(
                            subject="RaceW",
                            predicate="state",
                            obj="flag",
                            content=content,
                        )
                    ],
                    source_episode_id=episode_id,
                    now=T1,
                )
                await db.commit()
                return result

        outcomes = await asyncio.gather(
            _race("flag red"), _race("flag green"), return_exceptions=True
        )
        failures = [o for o in outcomes if isinstance(o, Exception)]
        assert not failures, (
            "Advisory lock must serialize the worker race — no exclusion "
            f"violation expected: {failures}"
        )
        assert sum(r.superseded_count for r in outcomes) == 1

        rows = await _spo_rows(engine, "RaceW")
        assert len(rows) == 2
        assert sum(r.valid_to is None for r in rows) == 1


class TestRollbackOnInsertFailure:
    """Scenario 7 — truncate-then-insert-failure rolls back atomically."""

    async def test_rollback_restores_old_fact(self, engine, db_session) -> None:
        user_id = _new_uuid()
        async with AsyncSession(engine) as db:
            await _seed_user(db, user_id)
            await db.commit()

        r1 = await _ingest(
            engine,
            facts=[_triple(content="Alice likes hiking")],
            user_id=user_id,
            now=T0,
        )
        old_fact_id = r1.created[0].id

        class _FailingInsertRepo(FactRepository):
            async def batch_create(self, *args: Any, **kwargs: Any) -> list[Any]:
                raise RuntimeError("simulated insert failure")

        db = AsyncSession(engine, expire_on_commit=False)
        repo = _FailingInsertRepo(db)
        service = FactInvalidationService(db=db, fact_repo=repo)
        with pytest.raises(RuntimeError, match="simulated insert failure"):
            await service.ingest_with_supersession(
                org_id=ORG_ID,
                project_id=PROJECT_ID,
                user_id=user_id,
                facts=[_triple(content="Alice loves hiking")],
                now=T1,
            )
        await db.rollback()
        await db.close()

        current = await _facts_at(engine, T1 + timedelta(minutes=1))
        assert len(current) == 1
        assert current[0].id == old_fact_id
        assert current[0].content == "Alice likes hiking"
        assert current[0].valid_to is None, (
            "Rollback must leave the old fact open — truncate is not committed"
        )


class TestWorkerPathSupersession:
    """Worker path (``batch_create_or_skip``) — production-like real-now flow.

    The repo honors the row's ``valid_from`` (M1 fix), so explicit
    past timestamps are preserved; these tests use the production-like
    ``now=None`` (real clock) flow.
    """

    async def test_worker_path_supersedes_without_constraint_error(
        self, engine, db_session
    ) -> None:
        user_id = _new_uuid()
        episode_id = _new_uuid()
        async with AsyncSession(engine) as db:
            await _seed_user(db, user_id)
            await _seed_episode(db, episode_id, PROJECT_ID, user_id)
            await db.commit()

        r1 = await _ingest(
            engine,
            facts=[
                _triple(
                    subject="Acme",
                    predicate="hq_in",
                    obj="NYC",
                    content="Acme HQ in NYC",
                )
            ],
            user_id=user_id,
            source_episode_id=episode_id,
            insert_mode="batch_create_or_skip",
        )
        assert r1.inserted_count == 1

        r2 = await _ingest(
            engine,
            facts=[
                _triple(
                    subject="Acme",
                    predicate="hq_in",
                    obj="NYC",
                    content="Acme HQ moved to NYC",
                )
            ],
            user_id=user_id,
            source_episode_id=episode_id,
            insert_mode="batch_create_or_skip",
        )
        assert r2.superseded_count == 1

        rows = await _spo_rows(engine, "Acme")
        assert len(rows) == 2
        # Old fact closed at the supersession instant; new fact starts at
        # or after it — no overlap, so the constraint never fired.
        assert rows[0].valid_to is not None
        assert rows[0].valid_to <= rows[1].valid_from
        assert rows[1].valid_to is None


class TestSearchExcludesSuperseded:
    """Scenario 9 — vector & BM25 legs apply the effective-at predicate."""

    async def test_vector_and_bm25_exclude_superseded(self, engine, db_session) -> None:
        user_id = _new_uuid()
        async with AsyncSession(engine) as db:
            await _seed_user(db, user_id)
            await db.commit()

        r1 = await _ingest(
            engine,
            facts=[
                _triple(
                    subject="Cobalt",
                    predicate="trend",
                    obj="market",
                    content="Cobalt mining output doubled",
                )
            ],
            user_id=user_id,
            now=T0,
        )
        old_id = r1.created[0].id
        r2 = await _ingest(
            engine,
            facts=[
                _triple(
                    subject="Cobalt",
                    predicate="trend",
                    obj="market",
                    content="Cobalt mining output tripled",
                )
            ],
            user_id=user_id,
            now=T1,
        )
        new_id = r2.created[0].id

        vec = [0.0] * 1536
        async with AsyncSession(engine) as db:
            await db.execute(
                sa_text("UPDATE facts SET embedding = :vec WHERE id = :id"),
                {"vec": vec, "id": old_id},
            )
            await db.execute(
                sa_text("UPDATE facts SET embedding = :vec WHERE id = :id"),
                {"vec": vec, "id": new_id},
            )
            await db.commit()

        db = AsyncSession(engine, expire_on_commit=False)
        try:
            retriever = HybridRetriever(db=db, org_id=ORG_ID, graph_backends=[])
            retriever._embed_query = AsyncMock(return_value=vec)

            # Token present in both facts; only the current one returns.
            vector_hits = await retriever._vector_search_facts(
                "cobalt", PROJECT_ID, limit=50
            )
            vector_ids = {str(h["id"]) for h in vector_hits}
            assert str(new_id) in vector_ids
            assert str(old_id) not in vector_ids, (
                "Superseded fact must not appear in vector search at 'now'"
            )

            bm25_hits = await retriever._bm25_search_facts(
                "cobalt mining output", PROJECT_ID, limit=50
            )
            bm25_ids = {str(h["id"]) for h in bm25_hits}
            assert str(new_id) in bm25_ids
            assert str(old_id) not in bm25_ids, (
                "Superseded fact must not appear in BM25 search at 'now'"
            )
        finally:
            await db.close()


class TestEnrichmentBitUntouched:
    """Scenario 13 — supersession is episode-agnostic (no ping-pong)."""

    async def test_supersession_does_not_touch_episode_enrichment(
        self, engine, db_session
    ) -> None:
        user_id = _new_uuid()
        episode_id = _new_uuid()
        async with AsyncSession(engine) as db:
            await _seed_user(db, user_id)
            await _seed_episode(db, episode_id, PROJECT_ID, user_id)
            await db.execute(
                sa_text("UPDATE episodes SET enrichment_status = 4 WHERE id = :eid"),
                {"eid": episode_id},
            )
            await db.commit()

        # A supersession on a fact FROM this episode.
        await _ingest(
            engine,
            facts=[
                _triple(
                    subject="Acme",
                    predicate="hq_in",
                    obj="NYC",
                    content="Acme HQ in NYC",
                )
            ],
            user_id=user_id,
            source_episode_id=episode_id,
            now=T0,
        )
        await _ingest(
            engine,
            facts=[
                _triple(
                    subject="Acme",
                    predicate="hq_in",
                    obj="NYC",
                    content="Acme HQ moved to NYC",
                )
            ],
            user_id=user_id,
            source_episode_id=episode_id,
            now=T1,
        )

        # Episode bitmask untouched — otherwise reconcile_enrichment could
        # re-extract → supersede → loop.
        async with AsyncSession(engine) as db:
            result = await db.execute(
                sa_text("SELECT enrichment_status FROM episodes WHERE id = :eid"),
                {"eid": episode_id},
            )
            (status,) = result.fetchone()
        assert status == 4, "Supersession must not clear episode enrichment bits"


class TestOrgScoping:
    """Scenario 14 — conflict scan respects organization_id."""

    async def test_same_spo_other_org_does_not_supersede(
        self, engine, db_session
    ) -> None:
        org_a, org_b = ORG_ID, _new_uuid()
        project_a, project_b = PROJECT_ID, _new_uuid()
        user_a, user_b = _new_uuid(), _new_uuid()

        async with AsyncSession(engine) as db:
            await _seed_org(db, org_b)
            await _seed_user(db, user_a, org_id=org_a)
            await _seed_user(db, user_b, org_id=org_b)
            await _seed_project(db, project_a, org_id=org_a)
            await _seed_project(db, project_b, org_id=org_b)
            await db.commit()

        await _ingest(
            engine,
            facts=[
                _triple(
                    subject="Shared",
                    predicate="owns",
                    obj="Asset",
                    content="Shared owns Asset v1",
                )
            ],
            user_id=user_a,
            project_id=project_a,
            org_id=org_a,
            now=T0,
        )
        r_b = await _ingest(
            engine,
            facts=[
                _triple(
                    subject="Shared",
                    predicate="owns",
                    obj="Asset",
                    content="Shared owns Asset v2",
                )
            ],
            user_id=user_b,
            project_id=project_b,
            org_id=org_b,
            now=T1,
        )
        assert r_b.superseded_count == 0, (
            "Same SPO in a different org must not supersede org A's fact"
        )

        a_rows = await _facts_at(
            engine, T1 + timedelta(minutes=1), project_id=project_a, org_id=org_a
        )
        assert [f.content for f in a_rows] == ["Shared owns Asset v1"]
        assert a_rows[0].valid_to is None

        b_rows = await _facts_at(
            engine, T1 + timedelta(minutes=1), project_id=project_b, org_id=org_b
        )
        assert [f.content for f in b_rows] == ["Shared owns Asset v2"]


class TestContextCachePurge:
    """Supersession purges the project's context-cache prefix."""

    async def test_supersession_purges_context_cache(
        self, engine, db_session, redis_client: Any
    ) -> None:
        user_id = _new_uuid()
        async with AsyncSession(engine) as db:
            await _seed_user(db, user_id)
            await db.commit()

        cache = CacheService(redis_client, default_ttl=30)
        await cache.set(f"ctx:{ORG_ID}:{PROJECT_ID}:ab12", "stale-context")
        await cache.set(f"ctx:{ORG_ID}:{PROJECT_ID}:cd34", "stale-context-2")

        db = AsyncSession(engine, expire_on_commit=False)
        try:
            repo = FactRepository(db)
            service = FactInvalidationService(
                db=db, fact_repo=repo, cache_service=cache
            )
            await service.ingest_with_supersession(
                org_id=ORG_ID,
                project_id=PROJECT_ID,
                user_id=user_id,
                facts=[_triple(content="Alice likes hiking")],
                now=T0,
            )
            await service.ingest_with_supersession(
                org_id=ORG_ID,
                project_id=PROJECT_ID,
                user_id=user_id,
                facts=[_triple(content="Alice loves hiking")],
                now=T1,
            )
            await db.commit()
            # M3: the context-cache purge is deferred until after commit and
            # dispatched as a task — yield so it completes before asserting.
            await asyncio.sleep(0)
        finally:
            await db.close()

        assert await redis_client.get(f"ctx:{ORG_ID}:{PROJECT_ID}:ab12") is None
        assert await redis_client.get(f"ctx:{ORG_ID}:{PROJECT_ID}:cd34") is None
