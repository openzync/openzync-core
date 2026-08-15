"""Integration tests — graph-edge expiry on fact supersession (Phase 3).

Runs against the testcontainers PostgreSQL stack (shared ``engine`` +
``db_session`` fixtures) and exercises the full sync path with REAL
transactions:

* Cross-form supersession (the headline fix): a string-identity (API)
  fact supersedes an entity-linked fact → the old entity's edge is
  expired at the supersession instant and excluded from
  ``list_entity_edges`` / ``traverse(at_time=...)`` (scenarios 1 + 7).
* Same-key supersession keeps the edge alive (D1 case 3, scenario 6).
* Retraction of the sole supporting fact expires the edge (scenario 8).
* Ingest rollback leaves the edge active — no phantom expiry (scenario 9).
* In-batch entity+literal duplicates collapse to one row (scenario 4).
* Reconcile anti-join: active edge without an active fact → expiry
  enqueued; with a matching active fact → kept (scenario 12).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.graph_backend.postgres import PostgresGraphBackend
from repositories.fact_repository import FactRepository
from services.fact_invalidation_service import FactInvalidationService
from services.graph_edge_sync_service import GraphEdgeSyncService
from workers.tasks.reconcile_graph_edges import reconcile_graph_edges

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
T1 = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)
T2 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


async def _seed_user(db: AsyncSession, user_id: UUID) -> None:
    await db.execute(
        sa_text(
            "INSERT INTO users (id, organization_id, external_id, name, "
            "role, is_active) "
            "VALUES (:uid, :org_id, :eid, :name, 'member', true) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {
            "uid": user_id,
            "org_id": ORG_ID,
            "eid": f"edge-exp-{user_id}",
            "name": "Edge Expiry Test User",
        },
    )


async def _make_graph(
    engine,
    *,
    src_name: str,
    tgt_name: str,
    rel: str,
    valid_from: datetime | None = None,
) -> dict:
    """Create two entities + one edge with the Postgres backend (real code)."""
    db = AsyncSession(engine, expire_on_commit=False)
    try:
        backend = PostgresGraphBackend(db=db)
        src = await backend.create_entity(
            ORG_ID, PROJECT_ID, name=src_name, entity_type="person"
        )
        tgt = await backend.create_entity(
            ORG_ID, PROJECT_ID, name=tgt_name, entity_type="brand"
        )
        rel_row = await backend.create_relationship(
            ORG_ID,
            PROJECT_ID,
            source_id=UUID(src["id"]),
            target_id=UUID(tgt["id"]),
            relationship_type=rel,
            confidence=1.0,
            valid_from=valid_from,
        )
        await db.commit()
        return {
            "src_id": UUID(src["id"]),
            "tgt_id": UUID(tgt["id"]),
            "edge_id": UUID(rel_row["id"]),
        }
    finally:
        await db.close()


async def _edge_invalid_at(
    engine, src_id: UUID, tgt_id: UUID, rel: str
) -> datetime | None:
    async with AsyncSession(engine) as db:
        result = await db.execute(
            sa_text(
                "SELECT invalid_at FROM graph_relationships "
                "WHERE source_id = :src AND target_id = :tgt "
                "AND relationship_type = :rel"
            ),
            {"src": str(src_id), "tgt": str(tgt_id), "rel": rel},
        )
        row = result.fetchone()
        return row[0] if row else None


async def _edge_ids_for(engine, entity_id: UUID) -> list[str]:
    async with AsyncSession(engine) as db:
        backend = PostgresGraphBackend(db=db)
        listing = await backend.list_entity_edges(ORG_ID, PROJECT_ID, entity_id)
        return [e["id"] for e in listing["items"]]


async def _await_effect(predicate, timeout_s: float = 10.0) -> Any:
    """Yield to the event loop until the post-commit effect task lands."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        value = await predicate()
        if value is not None:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError("post-commit graph-sync effect did not complete in time")


def _entity_triple(
    src_entity: UUID, tgt_entity: UUID, content: str
) -> dict[str, Any]:
    return {
        "subject": "Robbie",
        "predicate": "wears",
        "object": "Adidas",
        "content": content,
        "confidence": 1.0,
        "subject_entity_id": str(src_entity),
        "object_entity_id": str(tgt_entity),
    }


def _literal_triple(content: str) -> dict[str, Any]:
    return {
        "subject": "Robbie",
        "predicate": "wears",
        "object": "Adidas",
        "content": content,
        "confidence": 1.0,
    }


class TestCrossFormSupersessionExpiresEdge:
    """Scenario 1 + 7 — the headline fix end-to-end against real PG."""

    async def test_string_supersession_expires_old_entity_edge(
        self, engine, db_session
    ) -> None:
        """API (string) supersession of an entity fact expires the old edge
        at the supersession instant; the successor is literal → D1 case 2."""
        user_id = uuid4()
        async with AsyncSession(engine) as db:
            await _seed_user(db, user_id)
            await db.commit()

        graph = await _make_graph(
            engine, src_name="Robbie", tgt_name="Adidas", rel="wears", valid_from=T0
        )
        assert await _edge_ids_for(engine, graph["src_id"]), (
            "edge must exist post-enrichment"
        )

        # ── Pre-supersession snapshot: the edge is traversable at T0 ──────
        db = AsyncSession(engine, expire_on_commit=False)
        try:
            backend = PostgresGraphBackend(db=db)
            pre = await backend.traverse(
                ORG_ID, PROJECT_ID, graph["src_id"], max_depth=1,
                at_time=T0 + timedelta(minutes=1),
            )
            assert str(graph["tgt_id"]) in {n["id"] for n in pre}, (
                "active edge must be traversed at as_of before the supersession"
            )
        finally:
            await db.close()

        # ── v1: entity-linked fact asserts the edge ─────────────────────
        db = AsyncSession(engine, expire_on_commit=False)
        try:
            backend = PostgresGraphBackend(db=db)
            service = FactInvalidationService(
                db=db,
                fact_repo=FactRepository(db),
                graph_sync=GraphEdgeSyncService(backends=[backend]),
            )
            r1 = await service.ingest_with_supersession(
                org_id=ORG_ID,
                project_id=PROJECT_ID,
                user_id=user_id,
                facts=[
                    _entity_triple(
                        graph["src_id"], graph["tgt_id"], "Robbie wears Adidas"
                    )
                ],
                now=T0,
            )
            assert r1.inserted_count == 1
            await db.commit()
        finally:
            await db.close()

        # ── v2: string-identity ingest supersedes (different content) ───
        db = AsyncSession(engine, expire_on_commit=False)
        try:
            backend = PostgresGraphBackend(db=db)
            service = FactInvalidationService(
                db=db,
                fact_repo=FactRepository(db),
                graph_sync=GraphEdgeSyncService(backends=[backend]),
            )
            r2 = await service.ingest_with_supersession(
                org_id=ORG_ID,
                project_id=PROJECT_ID,
                user_id=user_id,
                facts=[_literal_triple("Robbie definitely wears Adidas")],
                now=T1,
            )
            assert r2.superseded_count == 1, (
                "string-identity ingest MUST supersede the entity-linked "
                "fact (headline fix)"
            )
            await db.commit()
        finally:
            await db.close()

        # ── The D1 case-2 expiry lands (post-commit effect, fresh session) ──
        invalid_at = await _await_effect(
            lambda: _edge_invalid_at(engine, graph["src_id"], graph["tgt_id"], "wears")
        )
        assert invalid_at == T1, "edge must be invalidated at the supersession instant"

        # Edge gone from the active listing.
        assert await _edge_ids_for(engine, graph["src_id"]) == []

        # Bitemporal exclusion: invalidated at T1, the edge is still
        # traversable at an as-of before T1 and excluded only at/after T1.
        db = AsyncSession(engine, expire_on_commit=False)
        try:
            backend = PostgresGraphBackend(db=db)
            before_t1 = await backend.traverse(
                ORG_ID, PROJECT_ID, graph["src_id"], max_depth=1,
                at_time=T0 + timedelta(minutes=1),
            )
            after_t1 = await backend.traverse(
                ORG_ID, PROJECT_ID, graph["src_id"], max_depth=1,
                at_time=T1 + timedelta(minutes=1),
            )
            assert str(graph["tgt_id"]) in {n["id"] for n in before_t1}, (
                "as-of before the invalidation instant must still reach the neighbour"
            )
            assert str(graph["tgt_id"]) not in {n["id"] for n in after_t1}
        finally:
            await db.close()

    async def test_retrieve_graph_as_of_excludes_superseded_edge(
        self, engine, db_session
    ) -> None:
        """Scenario 13 (Postgres leg) — retrieve_graph(as_of) follows the
        supersession boundary of a cross-form supersession."""
        user_id = uuid4()
        async with AsyncSession(engine) as db:
            await _seed_user(db, user_id)
            await db.commit()

        graph = await _make_graph(
            engine,
            src_name="AsOfSource",
            tgt_name="AsOfTarget",
            rel="wears",
            valid_from=T0,
        )

        db = AsyncSession(engine, expire_on_commit=False)
        try:
            backend = PostgresGraphBackend(db=db)
            service = FactInvalidationService(
                db=db,
                fact_repo=FactRepository(db),
                graph_sync=GraphEdgeSyncService(backends=[backend]),
            )
            await service.ingest_with_supersession(
                org_id=ORG_ID,
                project_id=PROJECT_ID,
                user_id=user_id,
                facts=[_entity_triple(graph["src_id"], graph["tgt_id"], "wears")],
                now=T0,
            )
            await db.commit()
        finally:
            await db.close()

        # Pre-supersession: the neighbour is reachable at distance 1.
        db = AsyncSession(engine, expire_on_commit=False)
        try:
            backend = PostgresGraphBackend(db=db)
            at_t0_before = await backend.retrieve_graph(
                ORG_ID, PROJECT_ID, query="AsOfSource", as_of=T0 + timedelta(minutes=1)
            )
            assert any(n["distance"] == 1 for n in at_t0_before), at_t0_before
        finally:
            await db.close()

        # Supersede cross-form (entity fact → literal successor).
        db = AsyncSession(engine, expire_on_commit=False)
        try:
            backend = PostgresGraphBackend(db=db)
            service = FactInvalidationService(
                db=db,
                fact_repo=FactRepository(db),
                graph_sync=GraphEdgeSyncService(backends=[backend]),
            )
            await service.ingest_with_supersession(
                org_id=ORG_ID,
                project_id=PROJECT_ID,
                user_id=user_id,
                facts=[_literal_triple("AsOfSource wears AsOfTarget now")],
                now=T1,
            )
            await db.commit()
        finally:
            await db.close()

        await _await_effect(
            lambda: _edge_invalid_at(engine, graph["src_id"], graph["tgt_id"], "wears")
        )

        # Post-supersession (invalid_at = T1): bitemporal — at an as-of
        # before the invalidation instant the edge is still reachable at
        # distance 1; excluded only at as_of >= T1.
        db = AsyncSession(engine, expire_on_commit=False)
        try:
            backend = PostgresGraphBackend(db=db)
            at_t0 = await backend.retrieve_graph(
                ORG_ID, PROJECT_ID, query="AsOfSource", as_of=T0 + timedelta(minutes=1)
            )
            at_t1 = await backend.retrieve_graph(
                ORG_ID, PROJECT_ID, query="AsOfSource", as_of=T1
            )
            at_t2 = await backend.retrieve_graph(
                ORG_ID, PROJECT_ID, query="AsOfSource", as_of=T2
            )
            assert any(n["distance"] == 1 for n in at_t0), (
                "as-of before the invalidation instant must still see the edge"
            )
            assert all(n["distance"] == 0 for n in at_t1), at_t1
            assert all(n["distance"] == 0 for n in at_t2), at_t2
        finally:
            await db.close()


class TestSameKeySupersessionKeepsEdge:
    """Scenario 6 — D1 case 3: successor re-asserts the same edge key."""

    async def test_edge_survives_same_entity_supersession(
        self, engine, db_session
    ) -> None:
        user_id = uuid4()
        async with AsyncSession(engine) as db:
            await _seed_user(db, user_id)
            await db.commit()

        graph = await _make_graph(
            engine, src_name="KeepSrc", tgt_name="KeepTgt", rel="wears", valid_from=T0
        )

        def _entity(content: str) -> dict[str, Any]:
            return _entity_triple(graph["src_id"], graph["tgt_id"], content)

        # v1 then v2 — both entity-linked, SAME UUIDs, different content.
        for instant, content in ((T0, "v1"), (T1, "v2")):
            db = AsyncSession(engine, expire_on_commit=False)
            try:
                backend = PostgresGraphBackend(db=db)
                service = FactInvalidationService(
                    db=db,
                    fact_repo=FactRepository(db),
                    graph_sync=GraphEdgeSyncService(backends=[backend]),
                )
                result = await service.ingest_with_supersession(
                    org_id=ORG_ID,
                    project_id=PROJECT_ID,
                    user_id=user_id,
                    facts=[_entity(content)],
                    now=instant,
                )
                assert result.inserted_count == 1
                await db.commit()
            finally:
                await db.close()

        # D1 case 3 — the successor re-asserts the same edge key: no expiry.
        await asyncio.sleep(0.2)  # give any (incorrect) expiry time to land
        assert (
            await _edge_invalid_at(engine, graph["src_id"], graph["tgt_id"], "wears")
            is None
        )
        assert await _edge_ids_for(engine, graph["src_id"]), (
            "same-key supersession must NOT expire the edge"
        )


class TestRetractionExpiresEdge:
    """Scenario 8 — retracting the sole supporting fact expires the edge."""

    async def test_notify_retraction_expires_edge(self, engine, db_session) -> None:
        user_id = uuid4()
        async with AsyncSession(engine) as db:
            await _seed_user(db, user_id)
            await db.commit()

        graph = await _make_graph(
            engine,
            src_name="RetractSrc",
            tgt_name="RetractTgt",
            rel="wears",
            valid_from=T0,
        )

        db = AsyncSession(engine, expire_on_commit=False)
        try:
            backend = PostgresGraphBackend(db=db)
            service = FactInvalidationService(
                db=db,
                fact_repo=FactRepository(db),
                graph_sync=GraphEdgeSyncService(backends=[backend]),
            )
            result = await service.ingest_with_supersession(
                org_id=ORG_ID,
                project_id=PROJECT_ID,
                user_id=user_id,
                facts=[
                    _entity_triple(
                        graph["src_id"], graph["tgt_id"], "supports edge"
                    )
                ],
                now=T0,
            )
            await db.commit()
            old_fact = result.created[0]

            # Retract — no successor re-asserts the edge → D1 case 1.
            db2 = AsyncSession(engine, expire_on_commit=False)
            try:
                backend2 = PostgresGraphBackend(db=db2)
                service2 = FactInvalidationService(
                    db=db2,
                    fact_repo=FactRepository(db2),
                    graph_sync=GraphEdgeSyncService(backends=[backend2]),
                )
                service2.notify_retraction(
                    org_id=ORG_ID, project_id=PROJECT_ID, old_fact=old_fact, at_time=T1
                )
                await db2.commit()
            finally:
                await db2.close()
        finally:
            await db.close()

        invalid_at = await _await_effect(
            lambda: _edge_invalid_at(engine, graph["src_id"], graph["tgt_id"], "wears")
        )
        assert invalid_at == T1
        assert await _edge_ids_for(engine, graph["src_id"]) == []


class TestIngestRollbackKeepsEdge:
    """Scenario 9 — a rolled-back supersession emits no edge expiry."""

    async def test_rollback_leaves_edge_active(self, engine, db_session) -> None:
        user_id = uuid4()
        async with AsyncSession(engine) as db:
            await _seed_user(db, user_id)
            await db.commit()

        graph = await _make_graph(
            engine, src_name="RBSrc", tgt_name="RBTgt", rel="wears", valid_from=T0
        )

        db = AsyncSession(engine, expire_on_commit=False)
        try:
            backend = PostgresGraphBackend(db=db)
            service = FactInvalidationService(
                db=db,
                fact_repo=FactRepository(db),
                graph_sync=GraphEdgeSyncService(backends=[backend]),
            )
            await service.ingest_with_supersession(
                org_id=ORG_ID,
                project_id=PROJECT_ID,
                user_id=user_id,
                facts=[_entity_triple(graph["src_id"], graph["tgt_id"], "v1")],
                now=T0,
            )
            await db.commit()
        finally:
            await db.close()

        # Supersession that never commits — the effect must be dropped.
        db = AsyncSession(engine, expire_on_commit=False)
        try:
            backend = PostgresGraphBackend(db=db)
            service = FactInvalidationService(
                db=db,
                fact_repo=FactRepository(db),
                graph_sync=GraphEdgeSyncService(backends=[backend]),
            )
            await service.ingest_with_supersession(
                org_id=ORG_ID,
                project_id=PROJECT_ID,
                user_id=user_id,
                facts=[_literal_triple("RBSrc wears RBTgt (never committed)")],
                now=T1,
            )
            await db.rollback()  # ← the transaction dies; effects must die too
        finally:
            await db.close()

        await asyncio.sleep(0.2)  # give any (phantom) expiry time to land
        assert (
            await _edge_invalid_at(engine, graph["src_id"], graph["tgt_id"], "wears")
            is None
        )
        assert await _edge_ids_for(engine, graph["src_id"]), (
            "rolled-back supersession must not expire the edge"
        )


class TestInBatchCrossFormCollapse:
    """Scenario 4 — entity + literal duplicates of one assertion collapse."""

    async def test_entity_and_literal_duplicates_insert_once(
        self, engine, db_session
    ) -> None:
        user_id = uuid4()
        async with AsyncSession(engine) as db:
            await _seed_user(db, user_id)
            await db.commit()

        graph = await _make_graph(
            engine, src_name="DupSrc", tgt_name="DupTgt", rel="wears"
        )

        db = AsyncSession(engine, expire_on_commit=False)
        try:
            service = FactInvalidationService(db=db, fact_repo=FactRepository(db))
            result = await service.ingest_with_supersession(
                org_id=ORG_ID,
                project_id=PROJECT_ID,
                user_id=user_id,
                facts=[
                    _entity_triple(
                        graph["src_id"], graph["tgt_id"], "identical assertion"
                    ),
                    _literal_triple("identical assertion"),
                ],
                now=T0,
            )
            await db.commit()
        finally:
            await db.close()

        assert result.inserted_count == 1, (
            "entity+literal duplicates must collapse to ONE row"
        )
        assert result.skipped_count == 1

        async with AsyncSession(engine) as db:
            count = await db.execute(
                sa_text(
                    "SELECT count(*) FROM facts WHERE subject = 'Robbie' "
                    "AND predicate = 'wears' AND object = 'Adidas'"
                )
            )
            assert count.scalar_one() == 1


class TestReconcileAntiJoin:
    """Scenario 12 — the 5-minute safety net against real PostgreSQL."""

    async def test_active_edge_without_active_fact_enqueues_expiry(
        self, engine, db_session
    ) -> None:
        user_id = uuid4()
        async with AsyncSession(engine) as db:
            await _seed_user(db, user_id)
            await db.commit()

        graph = await _make_graph(
            engine, src_name="RecSrc", tgt_name="RecTgt", rel="works_at"
        )

        enqueued: list[dict] = []

        class _Redis:
            async def enqueue_job(self, task: str, **kwargs) -> str:
                enqueued.append({"task": task, "kwargs": kwargs})
                return "job-1"

        from core.db import get_async_session

        ctx = {
            "db_session_factory": get_async_session(engine),
            "redis": _Redis(),
            "_queue_name": "OpenZync:test:queue:low",
        }

        # No fact asserts the edge → the anti-join flags it stale.
        summary = await reconcile_graph_edges(ctx)

        assert "1 stale" in summary
        assert len(enqueued) == 1
        kwargs = enqueued[0]["kwargs"]
        assert enqueued[0]["task"] == "expire_graph_edges"
        assert kwargs["source_id"] == str(graph["src_id"])
        assert kwargs["target_id"] == str(graph["tgt_id"])
        assert kwargs["relationship_type"] == "works_at"
        assert kwargs["fact_id"] == str(graph["edge_id"])

    async def test_active_edge_with_active_fact_is_kept(
        self, engine, db_session
    ) -> None:
        user_id = uuid4()
        async with AsyncSession(engine) as db:
            await _seed_user(db, user_id)
            await db.commit()

        graph = await _make_graph(
            engine, src_name="KeepRecSrc", tgt_name="KeepRecTgt", rel="works_at"
        )

        # A live, open-ended fact asserts the exact edge key.
        db = AsyncSession(engine, expire_on_commit=False)
        try:
            service = FactInvalidationService(db=db, fact_repo=FactRepository(db))
            await service.ingest_with_supersession(
                org_id=ORG_ID,
                project_id=PROJECT_ID,
                user_id=user_id,
                facts=[
                    {
                        "subject": "KeepRecSrc",
                        "predicate": "works_at",
                        "object": "KeepRecTgt",
                        "content": "KeepRecSrc works_at KeepRecTgt",
                        "confidence": 1.0,
                        "subject_entity_id": str(graph["src_id"]),
                        "object_entity_id": str(graph["tgt_id"]),
                    }
                ],
                now=T0,
            )
            await db.commit()
        finally:
            await db.close()

        enqueued: list[dict] = []

        class _Redis:
            async def enqueue_job(self, task: str, **kwargs) -> str:
                enqueued.append({"task": task, "kwargs": kwargs})
                return "job-1"

        from core.db import get_async_session

        ctx = {
            "db_session_factory": get_async_session(engine),
            "redis": _Redis(),
            "_queue_name": "OpenZync:test:queue:low",
        }

        summary = await reconcile_graph_edges(ctx)

        assert summary == "No stale edges found"
        assert enqueued == [], "an edge asserted by an active fact must not expire"

    async def test_superseded_fact_releases_edge_for_reconcile(
        self, engine, db_session
    ) -> None:
        """Drift repair: cross-form supersession (missed by the sync) closes
        the fact while the edge stays active → the anti-join flags it stale."""
        user_id = uuid4()
        async with AsyncSession(engine) as db:
            await _seed_user(db, user_id)
            await db.commit()

        graph = await _make_graph(
            engine, src_name="DriftSrc", tgt_name="DriftTgt", rel="works_at"
        )

        db = AsyncSession(engine, expire_on_commit=False)
        try:
            service = FactInvalidationService(db=db, fact_repo=FactRepository(db))
            result = await service.ingest_with_supersession(
                org_id=ORG_ID,
                project_id=PROJECT_ID,
                user_id=user_id,
                facts=[
                    {
                        "subject": "DriftSrc",
                        "predicate": "works_at",
                        "object": "DriftTgt",
                        "content": "v1",
                        "confidence": 1.0,
                        "subject_entity_id": str(graph["src_id"]),
                        "object_entity_id": str(graph["tgt_id"]),
                    }
                ],
                now=T0,
            )
            await db.commit()
            fact_id = result.created[0].id

            # LITERAL successor supersedes WITHOUT graph_sync — simulates the
            # missed-sync drift the cron exists to repair: the old entity fact
            # is closed, no active fact asserts the edge, edge stays active.
            db2 = AsyncSession(engine, expire_on_commit=False)
            try:
                service2 = FactInvalidationService(
                    db=db2, fact_repo=FactRepository(db2)
                )
                result2 = await service2.ingest_with_supersession(
                    org_id=ORG_ID,
                    project_id=PROJECT_ID,
                    user_id=user_id,
                    facts=[
                        {
                            "subject": "DriftSrc",
                            "predicate": "works_at",
                            "object": "DriftTgt",
                            "content": "DriftSrc works_at DriftTgt (raw)",
                            "confidence": 1.0,
                        }
                    ],
                    now=T1,
                )
                assert result2.superseded_count == 1, (
                    "literal successor must supersede the entity fact (cross-form)"
                )
                await db2.commit()
            finally:
                await db2.close()
        finally:
            await db.close()

        # Drift present: edge active, first fact closed, no open entity fact.
        assert (
            await _edge_invalid_at(engine, graph["src_id"], graph["tgt_id"], "works_at")
            is None
        )
        async with AsyncSession(engine) as db:
            row = await db.execute(
                sa_text("SELECT valid_to FROM facts WHERE id = :fid"), {"fid": fact_id}
            )
            assert row.scalar_one() is not None, "superseded fact must be closed"

        enqueued: list[dict] = []

        class _Redis:
            async def enqueue_job(self, task: str, **kwargs) -> str:
                enqueued.append({"task": task, "kwargs": kwargs})
                return "job-1"

        from core.db import get_async_session

        ctx = {
            "db_session_factory": get_async_session(engine),
            "redis": _Redis(),
            "_queue_name": "OpenZync:test:queue:low",
        }
        summary = await reconcile_graph_edges(ctx)
        assert "1 stale" in summary, (
            "the anti-join must flag an edge with no open matching fact"
        )
        assert len(enqueued) == 1
        assert enqueued[0]["kwargs"]["fact_id"] == str(graph["edge_id"])

    async def test_future_valid_to_fact_keeps_edge(self, engine, db_session) -> None:
        """REGRESSION: a currently-valid fact whose window closes in the
        future (``valid_to > now``) must still sustain its edge. Fails against
        the old ``f.valid_to IS NULL`` anti-join predicate; passes with the
        effective-at ``f.valid_to > :now`` bound."""
        user_id = uuid4()
        async with AsyncSession(engine) as db:
            await _seed_user(db, user_id)
            await db.commit()

        now = datetime.now(UTC)
        graph = await _make_graph(
            engine, src_name="FutValidSrc", tgt_name="FutValidTgt", rel="works_at"
        )

        db = AsyncSession(engine, expire_on_commit=False)
        try:
            await FactRepository(db).batch_create(
                organization_id=ORG_ID,
                project_id=PROJECT_ID,
                user_id=user_id,
                facts=[
                    {
                        "subject": "FutValidSrc",
                        "predicate": "works_at",
                        "object": "FutValidTgt",
                        "content": "FutValidSrc works_at FutValidTgt",
                        "confidence": 1.0,
                        "subject_type": "entity",
                        "object_type": "entity",
                        "subject_entity_id": str(graph["src_id"]),
                        "object_entity_id": str(graph["tgt_id"]),
                        "valid_from": now - timedelta(days=30),
                        "valid_to": now + timedelta(days=30),
                    }
                ],
            )
            await db.commit()
        finally:
            await db.close()

        enqueued: list[dict] = []

        class _Redis:
            async def enqueue_job(self, task: str, **kwargs) -> str:
                enqueued.append({"task": task, "kwargs": kwargs})
                return "job-1"

        from core.db import get_async_session

        ctx = {
            "db_session_factory": get_async_session(engine),
            "redis": _Redis(),
            "_queue_name": "OpenZync:test:queue:low",
        }

        # Fact is effective at now and expires later → edge must be kept.
        summary = await reconcile_graph_edges(ctx)

        assert summary == "No stale edges found"
        assert enqueued == [], (
            "an edge asserted by a not-yet-expired fact must not expire"
        )

    async def test_future_dated_fact_keeps_edge(self, engine, db_session) -> None:
        """Pre-materialization branch: a fact whose window opens in the
        future (``valid_from > now``) but closes later still sustains the
        pre-created edge — the anti-join only checks ``valid_to``."""
        user_id = uuid4()
        async with AsyncSession(engine) as db:
            await _seed_user(db, user_id)
            await db.commit()

        now = datetime.now(UTC)
        graph = await _make_graph(
            engine, src_name="FutDatedSrc", tgt_name="FutDatedTgt", rel="works_at"
        )

        db = AsyncSession(engine, expire_on_commit=False)
        try:
            await FactRepository(db).batch_create(
                organization_id=ORG_ID,
                project_id=PROJECT_ID,
                user_id=user_id,
                facts=[
                    {
                        "subject": "FutDatedSrc",
                        "predicate": "works_at",
                        "object": "FutDatedTgt",
                        "content": "FutDatedSrc works_at FutDatedTgt",
                        "confidence": 1.0,
                        "subject_type": "entity",
                        "object_type": "entity",
                        "subject_entity_id": str(graph["src_id"]),
                        "object_entity_id": str(graph["tgt_id"]),
                        "valid_from": now + timedelta(days=7),
                        "valid_to": now + timedelta(days=60),
                    }
                ],
            )
            await db.commit()
        finally:
            await db.close()

        enqueued: list[dict] = []

        class _Redis:
            async def enqueue_job(self, task: str, **kwargs) -> str:
                enqueued.append({"task": task, "kwargs": kwargs})
                return "job-1"

        from core.db import get_async_session

        ctx = {
            "db_session_factory": get_async_session(engine),
            "redis": _Redis(),
            "_queue_name": "OpenZync:test:queue:low",
        }

        summary = await reconcile_graph_edges(ctx)

        assert summary == "No stale edges found"
        assert enqueued == [], "a future-dated fact window must still sustain the edge"

    async def test_past_window_fact_releases_edge(self, engine, db_session) -> None:
        """Post-``valid_to`` expiry: a fully elapsed fact window
        (``valid_to < now``) no longer asserts the edge → the anti-join
        flags it stale and enqueues the expiry with reconcile provenance."""
        user_id = uuid4()
        async with AsyncSession(engine) as db:
            await _seed_user(db, user_id)
            await db.commit()

        now = datetime.now(UTC)
        graph = await _make_graph(
            engine, src_name="PastWinSrc", tgt_name="PastWinTgt", rel="works_at"
        )

        db = AsyncSession(engine, expire_on_commit=False)
        try:
            await FactRepository(db).batch_create(
                organization_id=ORG_ID,
                project_id=PROJECT_ID,
                user_id=user_id,
                facts=[
                    {
                        "subject": "PastWinSrc",
                        "predicate": "works_at",
                        "object": "PastWinTgt",
                        "content": "PastWinSrc works_at PastWinTgt",
                        "confidence": 1.0,
                        "subject_type": "entity",
                        "object_type": "entity",
                        "subject_entity_id": str(graph["src_id"]),
                        "object_entity_id": str(graph["tgt_id"]),
                        "valid_from": now - timedelta(days=60),
                        "valid_to": now - timedelta(days=30),
                    }
                ],
            )
            await db.commit()
        finally:
            await db.close()

        enqueued: list[dict] = []

        class _Redis:
            async def enqueue_job(self, task: str, **kwargs) -> str:
                enqueued.append({"task": task, "kwargs": kwargs})
                return "job-1"

        from core.db import get_async_session

        ctx = {
            "db_session_factory": get_async_session(engine),
            "redis": _Redis(),
            "_queue_name": "OpenZync:test:queue:low",
        }

        summary = await reconcile_graph_edges(ctx)

        assert "1 stale" in summary, (
            "an edge whose fact window has fully elapsed must be flagged stale"
        )
        assert len(enqueued) == 1
        kwargs = enqueued[0]["kwargs"]
        assert enqueued[0]["task"] == "expire_graph_edges"
        assert kwargs["fact_id"] == str(graph["edge_id"]), (
            "reconcile-sourced expiries carry the edge id as provenance"
        )
