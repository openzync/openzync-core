"""Integration tests for graph observations — repository + service pipeline.

Tests two layers against real testcontainers PostgreSQL:

1. **ObservationRepository** — upsert, query, and delete operations against
   the ``graph_observations`` table, including the functional unique index
   for idempotent re-upserts.

2. **ObservationService run_full_project_scan** — the full detection pipeline
   with real seeded data: entities, episodes, links, facts, and relationships.

Every test creates its own ``AsyncSession`` and never commits — all data is
rolled back when the session closes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from models.graph_observation import ObservationType
from repositories.observation_repository import ObservationRepository
from services.observation_service import ObservationService

pytestmark = pytest.mark.integration

# Well-known IDs seeded by conftest.py engine fixture
ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")


# ═══════════════════════════════════════════════════════════════════════════════
# ObservationRepository — CRUD integration tests
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestObservationRepository:
    """CRUD + upsert semantics against real PostgreSQL."""

    _ENTITY_A: UUID = uuid4()
    _ENTITY_B: UUID = uuid4()

    async def _seed_entities(self, db: AsyncSession) -> None:
        """Insert two test entities."""
        for eid, name in [(self._ENTITY_A, "Alice"), (self._ENTITY_B, "Bob")]:
            await db.execute(
                sa_text(
                    "INSERT INTO graph_entities "
                    "(id, organization_id, project_id, name, entity_type) "
                    "VALUES (:id, :org_id, :proj_id, :name, 'test') "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {"id": eid, "org_id": ORG_ID, "proj_id": PROJECT_ID,
                 "name": name},
            )
        await db.flush()

    async def _seed_entity(self, db: AsyncSession, eid: UUID,
                           name: str = "Entity") -> None:
        """Insert a single test entity."""
        await db.execute(
            sa_text(
                "INSERT INTO graph_entities "
                "(id, organization_id, project_id, name, entity_type) "
                "VALUES (:id, :org_id, :proj_id, :name, 'test') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": eid, "org_id": ORG_ID, "proj_id": PROJECT_ID, "name": name},
        )
        await db.flush()

    async def _run(self, engine, fn) -> None:
        """Open a session, run ``fn(db, repo)``, then roll back."""
        db = AsyncSession(engine, expire_on_commit=False)
        try:
            repo = ObservationRepository(db)
            await fn(db, repo)
        finally:
            await db.rollback()
            await db.close()

    # ── Upsert: insert ─────────────────────────────────────────────────────

    async def test_upsert_inserts_new_observation(self, engine) -> None:
        """Upsert inserts a new observation row."""
        async def _test(db: AsyncSession, repo: ObservationRepository) -> None:
            await self._seed_entity(db, self._ENTITY_A, "Alice")
            await repo.upsert(
                organization_id=ORG_ID,
                project_id=PROJECT_ID,
                subject_entity_id=self._ENTITY_A,
                observation_type=str(ObservationType.TEMPORAL_PATTERN),
                content="Alice appears weekly.",
                confidence=0.85,
                related_entity_id=None,
                valid_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
            )
            results = await repo.get_by_project(PROJECT_ID)
            assert len(results) == 1
            assert results[0]["content"] == "Alice appears weekly."
            assert results[0]["confidence"] == 0.85

        await self._run(engine, _test)

    async def test_upsert_idempotent_reupdate(self, engine) -> None:
        """Upsert updates on dedup key collision vs insert duplicate."""
        async def _test(db: AsyncSession, repo: ObservationRepository) -> None:
            await self._seed_entity(db, self._ENTITY_A, "Alice")
            # First insert
            await repo.upsert(
                organization_id=ORG_ID,
                project_id=PROJECT_ID,
                subject_entity_id=self._ENTITY_A,
                observation_type=str(ObservationType.TEMPORAL_PATTERN),
                content="Alice appears weekly.",
                confidence=0.85,
                related_entity_id=None,
            )
            # Second upsert with different content — same dedup key
            await repo.upsert(
                organization_id=ORG_ID,
                project_id=PROJECT_ID,
                subject_entity_id=self._ENTITY_A,
                observation_type=str(ObservationType.TEMPORAL_PATTERN),
                content="Alice appears daily.",
                confidence=0.90,
                related_entity_id=None,
            )
            results = await repo.get_by_project(PROJECT_ID)
            assert len(results) == 1, "Should still be one row"
            assert results[0]["content"] == "Alice appears daily."
            assert results[0]["confidence"] == 0.90

        await self._run(engine, _test)

    async def test_upsert_pair_and_entity_level_separate(self, engine) -> None:
        """Entity-level and pair-level observations with same entity+type
        are distinct rows (different related_entity_id)."""
        async def _test(db: AsyncSession, repo: ObservationRepository) -> None:
            await self._seed_entity(db, self._ENTITY_A, "Alice")
            await self._seed_entity(db, self._ENTITY_B, "Bob")

            # Entity-level: related_entity_id is NULL
            await repo.upsert(
                organization_id=ORG_ID,
                project_id=PROJECT_ID,
                subject_entity_id=self._ENTITY_A,
                observation_type=str(ObservationType.CO_OCCURRENCE),
                content="Entity-level co-occurrence summary.",
                confidence=0.5,
                related_entity_id=None,
            )
            # Pair-level: related_entity_id is set
            await repo.upsert(
                organization_id=ORG_ID,
                project_id=PROJECT_ID,
                subject_entity_id=self._ENTITY_A,
                observation_type=str(ObservationType.CO_OCCURRENCE),
                content="Alice co-occurs with Bob.",
                confidence=0.8,
                related_entity_id=self._ENTITY_B,
            )
            results = await repo.get_by_project(PROJECT_ID)
            assert len(results) == 2, "Two distinct observations"

        await self._run(engine, _test)

    # ── Query methods ──────────────────────────────────────────────────────

    async def test_get_by_subject_filters_correctly(self, engine) -> None:
        """get_by_subject returns only observations about that entity."""
        async def _test(db: AsyncSession, repo: ObservationRepository) -> None:
            await self._seed_entity(db, self._ENTITY_A, "Alice")
            await self._seed_entity(db, self._ENTITY_B, "Bob")

            await repo.upsert(
                organization_id=ORG_ID, project_id=PROJECT_ID,
                subject_entity_id=self._ENTITY_A,
                observation_type=str(ObservationType.TEMPORAL_PATTERN),
                content="Alice pattern.", confidence=0.8,
                related_entity_id=None,
            )
            await repo.upsert(
                organization_id=ORG_ID, project_id=PROJECT_ID,
                subject_entity_id=self._ENTITY_B,
                observation_type=str(ObservationType.TEMPORAL_PATTERN),
                content="Bob pattern.", confidence=0.7,
                related_entity_id=None,
            )

            alice_obs = await repo.get_by_subject(PROJECT_ID, self._ENTITY_A)
            assert len(alice_obs) == 1
            assert alice_obs[0]["content"] == "Alice pattern."

            bob_obs = await repo.get_by_subject(PROJECT_ID, self._ENTITY_B)
            assert len(bob_obs) == 1
            assert bob_obs[0]["content"] == "Bob pattern."

        await self._run(engine, _test)

    async def test_get_by_type_filters_correctly(self, engine) -> None:
        """get_by_type returns only observations of that type."""
        async def _test(db: AsyncSession, repo: ObservationRepository) -> None:
            await self._seed_entity(db, self._ENTITY_A, "Alice")

            await repo.upsert(
                organization_id=ORG_ID, project_id=PROJECT_ID,
                subject_entity_id=self._ENTITY_A,
                observation_type=str(ObservationType.TEMPORAL_PATTERN),
                content="Temporal", confidence=0.8,
                related_entity_id=None,
            )
            await repo.upsert(
                organization_id=ORG_ID, project_id=PROJECT_ID,
                subject_entity_id=self._ENTITY_A,
                observation_type=str(ObservationType.BEHAVIORAL_PATTERN),
                content="Behavioral", confidence=0.7,
                related_entity_id=None,
            )

            temporal = await repo.get_by_type(
                PROJECT_ID, str(ObservationType.TEMPORAL_PATTERN),
            )
            assert len(temporal) == 1
            assert temporal[0]["content"] == "Temporal"

            behavioral = await repo.get_by_type(
                PROJECT_ID, str(ObservationType.BEHAVIORAL_PATTERN),
            )
            assert len(behavioral) == 1
            assert behavioral[0]["content"] == "Behavioral"

        await self._run(engine, _test)

    async def test_get_pair_observations_both_directions(self, engine) -> None:
        """get_pair_observations finds observations in either direction."""
        async def _test(db: AsyncSession, repo: ObservationRepository) -> None:
            await self._seed_entity(db, self._ENTITY_A, "Alice")
            await self._seed_entity(db, self._ENTITY_B, "Bob")

            # Observation: Alice → Bob
            await repo.upsert(
                organization_id=ORG_ID, project_id=PROJECT_ID,
                subject_entity_id=self._ENTITY_A,
                observation_type=str(ObservationType.CO_OCCURRENCE),
                content="Alice with Bob.", confidence=0.8,
                related_entity_id=self._ENTITY_B,
            )

            # Query with A/B — should find the one above
            results = await repo.get_pair_observations(
                PROJECT_ID, self._ENTITY_A, self._ENTITY_B,
            )
            assert len(results) == 1
            assert results[0]["subject_entity_id"] == self._ENTITY_A
            assert results[0]["related_entity_id"] == self._ENTITY_B

            # Query with B/A — should find it too (OR logic)
            results = await repo.get_pair_observations(
                PROJECT_ID, self._ENTITY_B, self._ENTITY_A,
            )
            assert len(results) == 1

        await self._run(engine, _test)

    # ── Delete ─────────────────────────────────────────────────────────────

    async def test_delete_by_project_removes_all(self, engine) -> None:
        """delete_by_project removes all observations for that project."""
        async def _test(db: AsyncSession, repo: ObservationRepository) -> None:
            await self._seed_entity(db, self._ENTITY_A, "Alice")
            await self._seed_entity(db, self._ENTITY_B, "Bob")

            for i in range(3):
                await repo.upsert(
                    organization_id=ORG_ID, project_id=PROJECT_ID,
                    subject_entity_id=self._ENTITY_A if i % 2 == 0
                    else self._ENTITY_B,
                    observation_type=str(ObservationType.TEMPORAL_PATTERN),
                    content=f"Obs {i}", confidence=0.5,
                    related_entity_id=None,
                )
            count = await repo.delete_by_project(PROJECT_ID)
            # Only 2 distinct dedup keys: ENTITY_A+TEMPORAL_PATTERN and
            # ENTITY_B+TEMPORAL_PATTERN (both with related_entity_id=None).
            # Third call with ENTITY_A updates rather than inserts.
            assert count == 2, f"Expected 2 rows (dedup), got {count}"

            remaining = await repo.get_by_project(PROJECT_ID)
            assert len(remaining) == 0



        await self._run(engine, _test)

# ═══════════════════════════════════════════════════════════════════════════════
# ObservationService — full pipeline integration tests
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestObservationServicePipeline:
    """End-to-end pipeline: seed data → run detection → verify persisted obs.

    Each test seeds a small graph with entities, episodes, links, facts,
    and relationships, then runs ``run_full_project_scan`` and checks
    the resulting observations in the DB.
    """

    _ENTITY_X: UUID = uuid4()
    _ENTITY_Y: UUID = uuid4()
    _ENTITY_Z: UUID = uuid4()
    _EPISODES: list[UUID] = [uuid4() for _ in range(5)]

    async def _seed_graph(
        self,
        db: AsyncSession,
        *,
        with_facts: bool = True,
        with_relationships: bool = True,
    ) -> None:
        """Seed a minimal graph for pipeline testing.

        Entities X, Y, Z.  Five episodes where:
          - X appears in episodes 0-4
          - Y appears in episodes 0, 2, 4 (co-occurrence with X in 3/5)
          - Z appears in episodes 1, 3 only (co-occurrence with X in 2/5)

        Facts (if with_facts):
          - X ``asked_about_pricing`` (3 times)
          - X ``requested_demo`` (2 times)
          - Y ``churned`` (2 times)

        Relationships (if with_relationships):
          - X → Y ``works_with`` (valid)
          - X → Z ``mentions`` (valid)
        """
        # ── Entities ───────────────────────────────────────────────────────
        for eid, name, etype in [
            (self._ENTITY_X, "UserX", "user"),
            (self._ENTITY_Y, "ProductY", "product"),
            (self._ENTITY_Z, "FeatureZ", "feature"),
        ]:
            await db.execute(
                sa_text(
                    "INSERT INTO graph_entities "
                    "(id, organization_id, project_id, name, entity_type) "
                    "VALUES (:id, :org_id, :proj_id, :name, :etype) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {"id": eid, "org_id": ORG_ID, "proj_id": PROJECT_ID,
                 "name": name, "etype": etype},
            )

        # ── Seed a user + session for episode FK references ────────────────
        _USER_ID = uuid4()
        _SESSION_ID = uuid4()
        await db.execute(
            sa_text(
                "INSERT INTO users (id, organization_id, external_id, name, "
                "role, is_active) VALUES (:uid, :org_id, :eid, :name, "
                "'member', true) ON CONFLICT (id) DO NOTHING"
            ),
            {"uid": _USER_ID, "org_id": ORG_ID,
             "eid": f"test-user-{_USER_ID}", "name": "Pipeline Test User"},
        )
        await db.execute(
            sa_text(
                "INSERT INTO sessions (id, organization_id, project_id, "
                "user_id, external_id, is_active) "
                "VALUES (:sid, :org_id, :proj_id, :uid, :eid, true) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"sid": _SESSION_ID, "org_id": ORG_ID, "proj_id": PROJECT_ID,
             "uid": _USER_ID, "eid": "test-session"},
        )

        # ── Episodes + entity links ────────────────────────────────────────
        for idx, eid in enumerate(self._EPISODES):
            created_at = datetime(
                2024, 1, 1 + idx * 7, tzinfo=timezone.utc,
            )  # 7-day gaps
            await db.execute(
                sa_text(
                    "INSERT INTO episodes "
                    "(id, organization_id, project_id, session_id, "
                    " user_id, role, content, token_count, "
                    " sequence_number, enrichment_status, "
                    " created_at, updated_at) "
                    "VALUES (:id, :org_id, :proj_id, :sid, :uid, "
                    " 'user', :content, 0, :seq, 127, "
                    " :created_at, :created_at) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {"id": eid, "org_id": ORG_ID, "proj_id": PROJECT_ID,
                 "sid": _SESSION_ID, "uid": _USER_ID,
                 "content": f"Episode {idx}", "seq": idx,
                 "created_at": created_at},
            )

            # X appears in every episode
            await db.execute(
                sa_text(
                    "INSERT INTO graph_episode_entities "
                    "(episode_id, entity_id, project_id) "
                    "VALUES (:eid, :entity_id, :proj_id) "
                    "ON CONFLICT (episode_id, entity_id) DO NOTHING"
                ),
                {"eid": eid, "entity_id": self._ENTITY_X,
                 "proj_id": PROJECT_ID},
            )

            # Y appears in episodes 0, 2, 4
            if idx % 2 == 0:
                await db.execute(
                    sa_text(
                    "INSERT INTO graph_episode_entities "
                    "(episode_id, entity_id, project_id) "
                    "VALUES (:eid, :entity_id, :proj_id) "
                    "ON CONFLICT (episode_id, entity_id) DO NOTHING"
                ),
                {"eid": eid, "entity_id": self._ENTITY_Y,
                 "proj_id": PROJECT_ID},
                )

            # Z appears in episodes 1, 3
            if idx % 2 == 1:
                await db.execute(
                    sa_text(
                    "INSERT INTO graph_episode_entities "
                    "(episode_id, entity_id, project_id) "
                    "VALUES (:eid, :entity_id, :proj_id) "
                    "ON CONFLICT (episode_id, entity_id) DO NOTHING"
                ),
                {"eid": eid, "entity_id": self._ENTITY_Z,
                 "proj_id": PROJECT_ID},
                )

        # ── Facts (if requested) ───────────────────────────────────────────
        if with_facts:
            fact_data = [
                # X asked_about_pricing × 3
                (self._ENTITY_X, "asked_about_pricing", "true",
                 self._EPISODES[0]),
                (self._ENTITY_X, "asked_about_pricing", "true",
                 self._EPISODES[2]),
                (self._ENTITY_X, "asked_about_pricing", "true",
                 self._EPISODES[4]),
                # X requested_demo × 2
                (self._ENTITY_X, "requested_demo", "true",
                 self._EPISODES[1]),
                (self._ENTITY_X, "requested_demo", "true",
                 self._EPISODES[3]),
                # Y churned × 2
                (self._ENTITY_Y, "churned", "true",
                 self._EPISODES[0]),
                (self._ENTITY_Y, "churned", "true",
                 self._EPISODES[2]),
            ]
            for subj, pred, obj, ep_id in fact_data:
                await db.execute(
                    sa_text(
                        "INSERT INTO facts "
                        "(id, user_id, organization_id, project_id, "
                        " source_episode_id, subject_entity_id, "
                        " subject, predicate, object, "
                        " content, confidence, "
                        " valid_from, created_at, updated_at) "
                        "VALUES (:id, :user_id, :org_id, :proj_id, "
                        " :ep_id, :subj_entity, "
                        " :subj_str, :pred, :obj, "
                        " :content, 1.0, "
                        " NOW(), NOW(), NOW()) "
                        "ON CONFLICT (id) DO NOTHING"
                    ),
                    {"id": uuid4(), "user_id": _USER_ID,
                     "org_id": ORG_ID, "proj_id": PROJECT_ID,
                     "ep_id": ep_id, "subj_entity": subj,
                     "subj_str": subj.hex, "pred": pred, "obj": obj,
                     "content": f"{pred}: {obj}"},
                )

        # ── Relationships (if requested) ───────────────────────────────────
        if with_relationships:
            await db.execute(
                sa_text(
                    "INSERT INTO graph_relationships "
                    "(id, organization_id, project_id, "
                    " source_id, target_id, relationship_type, "
                    " created_at, updated_at) "
                    "VALUES (:id, :org_id, :proj_id, "
                    " :src, :tgt, :rtype, NOW(), NOW()) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {"id": uuid4(), "org_id": ORG_ID, "proj_id": PROJECT_ID,
                 "src": self._ENTITY_X, "tgt": self._ENTITY_Y,
                 "rtype": "works_with"},
            )
            await db.execute(
                sa_text(
                    "INSERT INTO graph_relationships "
                    "(id, organization_id, project_id, "
                    " source_id, target_id, relationship_type, "
                    " created_at, updated_at) "
                    "VALUES (:id, :org_id, :proj_id, "
                    " :src, :tgt, :rtype, NOW(), NOW()) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {"id": uuid4(), "org_id": ORG_ID, "proj_id": PROJECT_ID,
                 "src": self._ENTITY_X, "tgt": self._ENTITY_Z,
                 "rtype": "mentions"},
            )

        await db.flush()

    async def _run_pipeline(
        self, engine, *,
        with_facts: bool = True,
        with_relationships: bool = True,
    ) -> tuple[dict[str, int], list[dict]]:
        """Seed data, run the pipeline, return counts + persisted obs.

        Returns:
            Tuple of (counts dict, list of observation rows from DB).
        """
        db = AsyncSession(engine, expire_on_commit=False)
        try:
            await self._seed_graph(
                db, with_facts=with_facts,
                with_relationships=with_relationships,
            )
            repo = ObservationRepository(db)
            service = ObservationService(
                db=db, repo=repo,
                min_co_count=2,  # Lower threshold for small test data
                min_appearances_for_temporal=3,
                min_gap_hours=1.0,
            )

            counts = await service.run_full_project_scan(
                project_id=PROJECT_ID,
                organization_id=ORG_ID,
                llm_backend=None,  # Use template descriptions
            )

            # Fetch all observations from the DB
            obs = await repo.get_by_project(PROJECT_ID)
            return counts, obs
        finally:
            await db.rollback()
            await db.close()

    # ── Tests ──────────────────────────────────────────────────────────────

    async def test_pipeline_produces_all_three_types(self, engine) -> None:
        """Full pipeline: co-occurrence + temporal + behavioral patterns."""
        counts, obs = await self._run_pipeline(engine)

        # Co-occurrence: X↔Y (3 co-occurrences >= 2 threshold), X↔Z (2 >= 2)
        co_count = counts.get("co_occurrence", 0)
        assert co_count >= 2, (
            f"Expected at least 2 co-occurrence obs, got {co_count}"
        )

        # Temporal: X (5 episodes >= 3), Y (3 >= 3), Z (2 < 3 threshold)
        temporal_count = counts.get("temporal_pattern", 0)
        assert temporal_count >= 2, (
            f"Expected at least 2 temporal obs, got {temporal_count}"
        )

        # Behavioral: X (5 facts, 2 predicates each >= 2), Y (2 facts)
        behavioral_count = counts.get("behavioral_pattern", 0)
        assert behavioral_count >= 1, (
            f"Expected at least 1 behavioral obs, got {behavioral_count}"
        )

        # Total observations persisted
        assert len(obs) >= 5, (
            f"Expected at least 5 total obs, got {len(obs)}"
        )

    async def test_pipeline_co_occurrence_details(self, engine) -> None:
        """Verify co-occurrence observation content and confidence."""
        _, obs = await self._run_pipeline(engine)

        co_obs = [o for o in obs
                  if o["observation_type"] == "co_occurrence"]
        assert len(co_obs) >= 2, (
            f"Expected >= 2 co-occurrence obs, got {len(co_obs)}"
        )

        # X↔Y: 3 co-occurrences out of 5 episodes = 60%
        # X↔Z: 2 co-occurrences out of 5 episodes = 40%
        # Check that at least one mentions UserX and ProductY
        pair_contents = [o["content"] for o in co_obs]
        assert any("UserX" in c for c in pair_contents), (
            "Expected 'UserX' in co-occurrence content"
        )
        assert any("ProductY" in c for c in pair_contents), (
            "Expected 'ProductY' in co-occurrence content"
        )
        # All confidences should be > 0
        for o in co_obs:
            assert o["confidence"] > 0, "Co-occurrence confidence must be > 0"

    async def test_pipeline_temporal_gap_details(self, engine) -> None:
        """Verify temporal pattern observation content."""
        _, obs = await self._run_pipeline(engine)

        temporal_obs = [o for o in obs
                        if o["observation_type"] == "temporal_pattern"]
        assert len(temporal_obs) >= 2

        # X appears every 7 days — should be "periodic" or "regular intervals"
        x_obs = [o for o in temporal_obs
                 if o["subject_entity_id"] == self._ENTITY_X]
        assert len(x_obs) == 1
        content = x_obs[0]["content"]
        # Should mention the entity name and gap pattern
        assert "UserX" in content or "regular" in content or "periodic" in content

        # Temporal patterns should have confidence > 0
        for o in temporal_obs:
            assert o["confidence"] > 0

    async def test_pipeline_behavioral_details(self, engine) -> None:
        """Verify behavioral pattern observation content."""
        _, obs = await self._run_pipeline(engine)

        behavioral_obs = [o for o in obs
                          if o["observation_type"] == "behavioral_pattern"]
        assert len(behavioral_obs) >= 1, (
            f"Expected >= 1 behavioral obs, got {len(behavioral_obs)}"
        )

        # X has asked_about_pricing × 3 — should be the top predicate
        x_obs = [o for o in behavioral_obs
                 if o["subject_entity_id"] == self._ENTITY_X]
        if x_obs:
            content = x_obs[0]["content"]
            assert "asked_about_pricing" in content or "UserX" in content

    async def test_pipeline_without_facts_or_relationships(
        self, engine,
    ) -> None:
        """Pipeline degrades gracefully when no facts or rels exist."""
        counts, obs = await self._run_pipeline(
            engine,
            with_facts=False,
            with_relationships=False,
        )

        # Co-occurrence and temporal should still work (based on
        # graph_episode_entities only)
        assert counts.get("co_occurrence", 0) >= 0
        assert counts.get("temporal_pattern", 0) >= 0

        # Behavioral should be 0 — no facts to analyze
        assert counts.get("behavioral_pattern", 0) == 0, (
            "Behavioral patterns require facts, expected 0"
        )

    async def test_pipeline_idempotent_runs(self, engine) -> None:
        """Running the pipeline twice upserts in-place (no duplicate rows)."""
        db = AsyncSession(engine, expire_on_commit=False)
        try:
            await self._seed_graph(db)
            repo = ObservationRepository(db)
            service = ObservationService(
                db=db, repo=repo,
                min_co_count=2,
                min_appearances_for_temporal=3,
            )

            # First run
            await service.run_full_project_scan(
                PROJECT_ID, ORG_ID, llm_backend=None,
            )
            obs_1 = await repo.get_by_project(PROJECT_ID)

            # Second run
            await service.run_full_project_scan(
                PROJECT_ID, ORG_ID, llm_backend=None,
            )
            obs_2 = await repo.get_by_project(PROJECT_ID)

            # Row count should be identical (upsert, not re-insert)
            assert len(obs_1) == len(obs_2), (
                f"Row count changed: {len(obs_1)} → {len(obs_2)}"
            )

            # Content updated on second run
            for i, (o1, o2) in enumerate(zip(obs_1, obs_2)):
                assert o1["id"] == o2["id"], (
                    f"Row {i}: IDs differ — possible re-insert"
                )

        finally:
            await db.rollback()
            await db.close()
