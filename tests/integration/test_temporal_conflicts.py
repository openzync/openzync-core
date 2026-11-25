"""Integration tests for temporal conflict resolution in facts.

Verifies the ``uq_facts_temporal_excl`` exclusion constraint and
temporal query semantics (``at_time`` / ``in_range``) against
testcontainers PostgreSQL.

Requires testcontainers PostgreSQL with the facts table.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.fact_repository import FactRepository

pytestmark = pytest.mark.integration

# Well-known IDs seeded by conftest.py engine fixture
ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")


@pytest.mark.asyncio
class TestFactTemporalExclusion:
    """Verify the ``uq_facts_temporal_excl`` exclusion constraint.

    The exclusion constraint prevents inserting two facts with the same
    ``(source_episode_id, subject, predicate, object)`` whose valid-time
    ranges overlap.  The constraint uses a GiST exclusion with
    ``tstzrange &&``, half-open ``'[)'`` semantics.

    Every test creates its own ``AsyncSession`` seeded with a test user
    and episode, then never commits — all data is rolled back when the
    session closes.
    """

    _USER_ID = UUID("00000000-0000-0000-0000-000000000010")
    _SESSION_ID = UUID("00000000-0000-0000-0000-000000000030")
    _EPISODE_ID = UUID("00000000-0000-0000-0000-000000000020")

    async def _seed_data(self, engine) -> AsyncSession:
        """Open a session and seed a test user + session + episode.

        Returns the session ready for the test.
        """
        # Use raw SQL inside a transaction that never commits —
        # rolling back the session cleans up all seed data.
        db = AsyncSession(engine, expire_on_commit=False)

        await db.execute(
            sa_text(
                "INSERT INTO users (id, external_id, name, role, is_active) "
                "VALUES (:uid, :eid, :name, 'member', true) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "uid": self._USER_ID,
                "eid": f"test-user-{self._USER_ID}",
                "name": "Temporal Test User",
            },
        )
        await db.execute(
            sa_text(
                "INSERT INTO sessions (id, user_id, project_id, external_id, is_active) "
                "VALUES (:sid, :uid, :proj_id, 'test-session', true) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "sid": self._SESSION_ID,
                "proj_id": PROJECT_ID,
                "uid": self._USER_ID,
            },
        )
        await db.execute(
            sa_text(
                "INSERT INTO episodes (id, user_id, project_id, session_id, "
                "role, content, token_count, sequence_number, enrichment_status) "
                "VALUES (:eid, :uid, :proj_id, :sid, 'user', 'test', 0, 1, 0) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "eid": self._EPISODE_ID,
                "proj_id": PROJECT_ID,
                "sid": self._SESSION_ID,
                "uid": self._USER_ID,
            },
        )
        await db.flush()
        return db

    async def _run_with_session(self, engine, fn) -> None:
        """Create a seeded session, run ``fn(db, repo)``, then roll back."""
        db = await self._seed_data(engine)
        try:
            repo = FactRepository(db)
            await fn(db, repo)
        finally:
            await db.rollback()
            await db.close()

    # ── Exclusion prevents overlapping ranges ────────────────────────────

    async def test_non_overlapping_same_triple(self, engine) -> None:
        """Same triple, disjoint valid ranges — both inserted."""
        async def _test(db, repo):
            facts = [
                {"subject": "A", "predicate": "knows", "object": "B",
                 "source_episode_id": self._EPISODE_ID,
                 "valid_from": datetime(2024, 1, 1, tzinfo=UTC),
                 "valid_to": datetime(2024, 3, 31, tzinfo=UTC)},
                {"subject": "A", "predicate": "knows", "object": "B",
                 "source_episode_id": self._EPISODE_ID,
                 "valid_from": datetime(2024, 4, 1, tzinfo=UTC),
                 "valid_to": datetime(2024, 6, 30, tzinfo=UTC)},
            ]
            created = await repo.batch_create(
                
                project_id=PROJECT_ID,
                user_id=self._USER_ID,
                facts=facts,
            )
            assert len(created) == 2, "Both disjoint facts should be inserted"

        await self._run_with_session(engine, _test)

    async def test_overlapping_same_triple_raises(self, engine) -> None:
        """Same triple, overlapping ranges — IntegrityError."""
        async def _test(db, repo):
            facts = [
                {"subject": "X", "predicate": "located_in", "object": "Y",
                 "source_episode_id": self._EPISODE_ID,
                 "valid_from": datetime(2024, 1, 1, tzinfo=UTC),
                 "valid_to": datetime(2024, 6, 30, tzinfo=UTC)},
                {"subject": "X", "predicate": "located_in", "object": "Y",
                 "source_episode_id": self._EPISODE_ID,
                 "valid_from": datetime(2024, 3, 1, tzinfo=UTC),
                 "valid_to": datetime(2024, 9, 30, tzinfo=UTC)},
            ]
            with pytest.raises(IntegrityError):
                await repo.batch_create(
                    
                    project_id=PROJECT_ID,
                    user_id=self._USER_ID,
                    facts=facts,
                )

        await self._run_with_session(engine, _test)

    async def test_adjacent_ranges_allowed(self, engine) -> None:
        """Adjacent ranges (end=start) are allowed — '[)' half-open."""
        async def _test(db, repo):
            facts = [
                {"subject": "P", "predicate": "reports_to", "object": "Q",
                 "source_episode_id": self._EPISODE_ID,
                 "valid_from": datetime(2024, 1, 1, tzinfo=UTC),
                 "valid_to": datetime(2024, 6, 1, tzinfo=UTC)},
                {"subject": "P", "predicate": "reports_to", "object": "Q",
                 "source_episode_id": self._EPISODE_ID,
                 "valid_from": datetime(2024, 6, 1, tzinfo=UTC),
                 "valid_to": datetime(2024, 12, 31, tzinfo=UTC)},
            ]
            created = await repo.batch_create(
                
                project_id=PROJECT_ID,
                user_id=self._USER_ID,
                facts=facts,
            )
            assert len(created) == 2, "Adjacent ranges should not conflict"

        await self._run_with_session(engine, _test)

    async def test_different_triple_same_range(self, engine) -> None:
        """Different triple, same range — no conflict."""
        async def _test(db, repo):
            facts = [
                {"subject": "A", "predicate": "knows", "object": "B",
                 "source_episode_id": self._EPISODE_ID,
                 "valid_from": datetime(2024, 1, 1, tzinfo=UTC),
                 "valid_to": datetime(2024, 12, 31, tzinfo=UTC)},
                {"subject": "C", "predicate": "knows", "object": "D",
                 "source_episode_id": self._EPISODE_ID,
                 "valid_from": datetime(2024, 1, 1, tzinfo=UTC),
                 "valid_to": datetime(2024, 12, 31, tzinfo=UTC)},
            ]
            created = await repo.batch_create(
                
                project_id=PROJECT_ID,
                user_id=self._USER_ID,
                facts=facts,
            )
            assert len(created) == 2, "Different triples never conflict"

        await self._run_with_session(engine, _test)

    async def test_invalidated_fact_bypasses_exclusion(self, engine) -> None:
        """Invalidated fact (invalid_at IS NOT NULL) is excluded from check."""
        async def _test(db, repo):
            # Insert first fact
            fact_dict = {
                "subject": "M", "predicate": "founded", "object": "N",
                "source_episode_id": self._EPISODE_ID,
                "valid_from": datetime(2024, 1, 1, tzinfo=UTC),
                "valid_to": datetime(2024, 12, 31, tzinfo=UTC),
            }
            [fact] = await repo.batch_create(
                
                project_id=PROJECT_ID,
                user_id=self._USER_ID,
                facts=[fact_dict],
            )

            # Soft-invalidate the first fact
            await db.execute(
                sa_text(
                    "UPDATE facts SET invalid_at = now(), updated_at = now() WHERE id = :fid"
                ),
                {"fid": fact.id},
            )
            await db.flush()

            # Insert same triple + range — should succeed since first is invalidated
            [second] = await repo.batch_create(
                
                project_id=PROJECT_ID,
                user_id=self._USER_ID,
                facts=[fact_dict],
            )
            assert second.id != fact.id

        await self._run_with_session(engine, _test)

    async def test_batch_create_on_conflict_skip(self, engine) -> None:
        """Overlapping facts with on_conflict='skip' — conflicting omitted."""
        async def _test(db, repo):
            # Insert first fact
            fact_dict = {
                "subject": "R", "predicate": "works_at", "object": "S",
                "source_episode_id": self._EPISODE_ID,
                "valid_from": datetime(2024, 1, 1, tzinfo=UTC),
                "valid_to": datetime(2024, 12, 31, tzinfo=UTC),
            }
            first = await repo.batch_create(
                
                project_id=PROJECT_ID,
                user_id=self._USER_ID,
                facts=[fact_dict],
            )

            # Overlapping second batch with on_conflict="skip"
            skipped = await repo.batch_create(
                
                project_id=PROJECT_ID,
                user_id=self._USER_ID,
                facts=[fact_dict,  # conflict
                       {"subject": "R2", "predicate": "works_at", "object": "S2",
                        "source_episode_id": self._EPISODE_ID,
                        "valid_from": datetime(2024, 1, 1, tzinfo=UTC),
                        "valid_to": datetime(2024, 12, 31, tzinfo=UTC)}],
                on_conflict="skip",
            )
            # Only the non-conflicting row should be returned
            assert len(skipped) == 1
            assert skipped[0].subject == "R2"

        await self._run_with_session(engine, _test)

    async def test_batch_create_on_conflict_error(self, engine) -> None:
        """Overlapping facts with on_conflict='error' (default) — raises."""
        async def _test(db, repo):
            fact_dict = {
                "subject": "T", "predicate": "manages", "object": "U",
                "source_episode_id": self._EPISODE_ID,
                "valid_from": datetime(2024, 1, 1, tzinfo=UTC),
                "valid_to": datetime(2024, 12, 31, tzinfo=UTC),
            }
            await repo.batch_create(
                
                project_id=PROJECT_ID,
                user_id=self._USER_ID,
                facts=[fact_dict],
            )

            # Same triple, same range — default (on_conflict="error")
            with pytest.raises(IntegrityError):
                await repo.batch_create(
                    
                    project_id=PROJECT_ID,
                    user_id=self._USER_ID,
                    facts=[fact_dict],
                )

        await self._run_with_session(engine, _test)

    async def test_open_ended_overlap(self, engine) -> None:
        """Open-ended fact (valid_to=NULL) conflicts with any overlapping range."""
        async def _test(db, repo):
            # First fact: open-ended (valid_to=NULL means "still active")
            await repo.batch_create(
                
                project_id=PROJECT_ID,
                user_id=self._USER_ID,
                facts=[{
                    "subject": "V", "predicate": "employs", "object": "W",
                    "source_episode_id": self._EPISODE_ID,
                    "valid_from": datetime(2024, 1, 1, tzinfo=UTC),
                    "valid_to": None,
                }],
            )

            # Second fact: same triple, overlapping range — should raise
            with pytest.raises(IntegrityError):
                await repo.batch_create(
                    
                    project_id=PROJECT_ID,
                    user_id=self._USER_ID,
                    facts=[{
                        "subject": "V", "predicate": "employs", "object": "W",
                        "source_episode_id": self._EPISODE_ID,
                        "valid_from": datetime(2024, 6, 1, tzinfo=UTC),
                        "valid_to": datetime(2024, 12, 31, tzinfo=UTC),
                    }],
                )

        await self._run_with_session(engine, _test)


@pytest.mark.asyncio
class TestTemporalQueries:
    """Integration tests for temporal query methods on facts.

    Tests ``get_facts_at_time()`` and ``get_facts_in_range()`` from
    ``FactRepository`` against a real PostgreSQL database.

    Every test seeds a user + session + episode + facts, runs the query,
    then rolls back — no cross-test pollution.
    """

    _USER_ID = UUID("00000000-0000-0000-0000-000000000010")
    _SESSION_ID = UUID("00000000-0000-0000-0000-000000000030")
    _EPISODE_ID = UUID("00000000-0000-0000-0000-000000000020")

    async def _seed_data(self, engine) -> AsyncSession:
        """Seed user + session + episode.  Returns the session."""
        db = AsyncSession(engine, expire_on_commit=False)

        await db.execute(
            sa_text(
                "INSERT INTO users (id, external_id, name, role, is_active) "
                "VALUES (:uid, :eid, :name, 'member', true) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "uid": self._USER_ID,
                "eid": f"test-user-{self._USER_ID}",
                "name": "Temporal Query Test User",
            },
        )
        await db.execute(
            sa_text(
                "INSERT INTO sessions (id, user_id, project_id, external_id, is_active) "
                "VALUES (:sid, :uid, :proj_id, 'test-session', true) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "sid": self._SESSION_ID,
                "proj_id": PROJECT_ID,
                "uid": self._USER_ID,
            },
        )
        await db.execute(
            sa_text(
                "INSERT INTO episodes (id, user_id, project_id, session_id, "
                "role, content, token_count, sequence_number, enrichment_status) "
                "VALUES (:eid, :uid, :proj_id, :sid, 'user', 'test', 0, 1, 0) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "eid": self._EPISODE_ID,
                "proj_id": PROJECT_ID,
                "sid": self._SESSION_ID,
                "uid": self._USER_ID,
            },
        )
        await db.flush()
        return db

    async def _seed_facts(
        self, db: AsyncSession, repo: FactRepository,
    ) -> None:
        """Insert a known set of facts for query tests.

        Fact A: valid 2024-01-01 to 2024-06-30
        Fact B: valid 2024-03-01 to 2024-09-30
        Fact C: open-ended (valid_from=2024-01-01, valid_to=NULL)
        Fact D: invalidated (valid 2024-01-01 to 2024-06-30, invalid_at set)
        """
        created = await repo.batch_create(
            
            project_id=PROJECT_ID,
            user_id=self._USER_ID,
            facts=[
                {"subject": "QA", "predicate": "test_time", "object": "A",
                 "source_episode_id": self._EPISODE_ID,
                 "valid_from": datetime(2024, 1, 1, tzinfo=UTC),
                 "valid_to": datetime(2024, 6, 30, tzinfo=UTC)},
                {"subject": "QB", "predicate": "test_time", "object": "B",
                 "source_episode_id": self._EPISODE_ID,
                 "valid_from": datetime(2024, 3, 1, tzinfo=UTC),
                 "valid_to": datetime(2024, 9, 30, tzinfo=UTC)},
                {"subject": "QC", "predicate": "test_time", "object": "C",
                 "source_episode_id": self._EPISODE_ID,
                 "valid_from": datetime(2024, 1, 1, tzinfo=UTC),
                 "valid_to": None},
                {"subject": "QD", "predicate": "test_time", "object": "D",
                 "source_episode_id": self._EPISODE_ID,
                 "valid_from": datetime(2024, 1, 1, tzinfo=UTC),
                 "valid_to": datetime(2024, 6, 30, tzinfo=UTC)},
            ],
        )

        # Invalidate fact D at a fixed instant inside its valid range
        # (2024-01-01..2024-06-30) and before every query bound below
        # (2024-05-01 at_time, 2024-02-01 in_range start).  A run-time
        # ``now()`` retraction is *after* those query times, so
        # ``invalid_at > :t`` keeps QD effective and the exclusion
        # assertions hold only while the wall clock predates the retraction.
        await db.execute(
            sa_text(
                "UPDATE facts SET invalid_at = :ts, updated_at = :ts WHERE id = :fid"
            ),
            {"fid": created[3].id, "ts": datetime(2024, 1, 15, tzinfo=UTC)},
        )
        await db.flush()

    # ── get_facts_at_time ────────────────────────────────────────────────────

    async def test_at_time_mid_range(self, engine) -> None:
        """Timestamp falls inside a fact's valid range → returned."""
        async def _test(db, repo):
            await self._seed_facts(db, repo)
            ts = datetime(2024, 5, 1, tzinfo=UTC)
            results = await repo.get_facts_at_time(PROJECT_ID, ts)
            subjects = {f.subject for f in results}
            assert "QA" in subjects  # [2024-01, 2024-06]
            assert "QB" in subjects  # [2024-03, 2024-09]
            assert "QC" in subjects  # [2024-01, ∞)
            assert "QD" not in subjects  # invalidated

        db = await self._seed_data(engine)
        try:
            repo = FactRepository(db)
            await _test(db, repo)
        finally:
            await db.rollback()
            await db.close()

    async def test_at_time_before_range(self, engine) -> None:
        """Timestamp before a fact's valid_from → not returned."""
        async def _test(db, repo):
            await self._seed_facts(db, repo)
            ts = datetime(2023, 12, 1, tzinfo=UTC)
            results = await repo.get_facts_at_time(PROJECT_ID, ts)
            assert len(results) == 0

        db = await self._seed_data(engine)
        try:
            repo = FactRepository(db)
            await _test(db, repo)
        finally:
            await db.rollback()
            await db.close()

    async def test_at_time_after_range_closed(self, engine) -> None:
        """Timestamp after a closed fact's valid_to → not returned,
        but open-ended facts are still included."""
        async def _test(db, repo):
            await self._seed_facts(db, repo)
            ts = datetime(2024, 12, 1, tzinfo=UTC)
            results = await repo.get_facts_at_time(PROJECT_ID, ts)
            subjects = {f.subject for f in results}
            assert "QA" not in subjects  # ends 2024-06
            assert "QB" not in subjects  # ends 2024-09
            assert "QC" in subjects  # open-ended — still active

        db = await self._seed_data(engine)
        try:
            repo = FactRepository(db)
            await _test(db, repo)
        finally:
            await db.rollback()
            await db.close()

    # ── get_facts_in_range ──────────────────────────────────────────────────

    async def test_in_range_partial_overlap(self, engine) -> None:
        """Query range partially overlaps a fact's range → returned."""
        async def _test(db, repo):
            await self._seed_facts(db, repo)
            start = datetime(2024, 2, 1, tzinfo=UTC)
            end = datetime(2024, 4, 1, tzinfo=UTC)
            results = await repo.get_facts_in_range(PROJECT_ID, start, end)
            subjects = {f.subject for f in results}
            assert "QA" in subjects  # overlaps [Feb, Apr)
            assert "QB" in subjects  # starts Mar, overlaps [Feb, Apr)
            assert "QC" in subjects  # open-ended
            assert "QD" not in subjects  # invalidated

        db = await self._seed_data(engine)
        try:
            repo = FactRepository(db)
            await _test(db, repo)
        finally:
            await db.rollback()
            await db.close()

    async def test_in_range_no_overlap(self, engine) -> None:
        """Query range does not overlap any closed fact → only open-ended
        facts are returned."""
        async def _test(db, repo):
            await self._seed_facts(db, repo)
            start = datetime(2025, 1, 1, tzinfo=UTC)
            end = datetime(2025, 6, 1, tzinfo=UTC)
            results = await repo.get_facts_in_range(PROJECT_ID, start, end)
            subjects = {f.subject for f in results}
            assert "QA" not in subjects  # ended 2024-06
            assert "QB" not in subjects  # ended 2024-09
            assert "QC" in subjects  # open-ended — always active
            assert "QD" not in subjects  # invalidated

        db = await self._seed_data(engine)
        try:
            repo = FactRepository(db)
            await _test(db, repo)
        finally:
            await db.rollback()
            await db.close()

    async def test_in_range_adjacent_no_overlap(self, engine) -> None:
        """Adjacent range (end=start) does not overlap ('[)' semantics)."""
        async def _test(db, repo):
            await self._seed_facts(db, repo)
            # Fact QA ends at 2024-06-30.  Query [2024-06-30, 2024-12-31)
            # should NOT include QA because '[)' excludes the end.
            start = datetime(2024, 6, 30, tzinfo=UTC)
            end = datetime(2024, 12, 31, tzinfo=UTC)
            results = await repo.get_facts_in_range(PROJECT_ID, start, end)
            subjects = {f.subject for f in results}
            assert "QA" not in subjects  # QA ends exactly at start of query
            assert "QC" in subjects  # open-ended, still active
            assert "QD" not in subjects  # invalidated

        db = await self._seed_data(engine)
        try:
            repo = FactRepository(db)
            await _test(db, repo)
        finally:
            await db.rollback()
            await db.close()
