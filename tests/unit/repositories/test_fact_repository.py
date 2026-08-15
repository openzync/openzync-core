"""Unit tests for FactRepository — fact CRUD, batch, search, and temporal queries."""

from __future__ import annotations

from datetime import UTC, datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Update

from core.exceptions import ValidationError
from repositories.fact_repository import FactRepository


pytestmark = pytest.mark.unit


class TestFactRepository:
    """FactRepository unit tests."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")
    USER_ID = UUID("00000000-0000-0000-0000-000000000003")
    FACT_ID = UUID("00000000-0000-0000-0000-000000000010")
    EPISODE_ID = UUID("00000000-0000-0000-0000-000000000020")
    SESSION_ID = UUID("00000000-0000-0000-0000-000000000030")

    @pytest.fixture
    def mock_db(self) -> AsyncMock:
        return AsyncMock(spec=AsyncSession)

    @pytest.fixture
    def repo(self, mock_db: AsyncMock) -> FactRepository:
        return FactRepository(db=mock_db)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _mock_fact(self, **overrides: object) -> MagicMock:
        f = MagicMock()
        f.id = overrides.get("id", self.FACT_ID)
        f.user_id = overrides.get("user_id", self.USER_ID)
        f.organization_id = overrides.get("organization_id", self.ORG_ID)
        f.project_id = overrides.get("project_id", self.PROJECT_ID)
        f.content = overrides.get("content", "Alice likes hiking")
        f.subject = overrides.get("subject", "Alice")
        f.predicate = overrides.get("predicate", "likes")
        f.object = overrides.get("object", "hiking")
        f.object_str = overrides.get("object_str", "hiking")
        f.confidence = overrides.get("confidence", 0.95)
        f.source_episode_id = overrides.get("source_episode_id", self.EPISODE_ID)
        f.valid_from = overrides.get("valid_from", datetime.now(timezone.utc))
        f.valid_to = overrides.get("valid_to", None)
        f.invalid_at = overrides.get("invalid_at", None)
        f.subject_type = overrides.get("subject_type", "literal")
        f.object_type = overrides.get("object_type", "literal")
        f.subject_entity_id = overrides.get("subject_entity_id", None)
        f.object_entity_id = overrides.get("object_entity_id", None)
        f.created_at = overrides.get("created_at", datetime.now(timezone.utc))
        return f

    # ── create ─────────────────────────────────────────────────────────────────

    async def test_create(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """create inserts and returns a new fact."""
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.create(
            user_id=self.USER_ID,
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            content="Alice likes hiking",
            subject="Alice",
            predicate="likes",
            obj="hiking",
            confidence=0.95,
            source_episode_id=self.EPISODE_ID,
        )

        assert result is not None
        mock_db.add.assert_called_once()
        mock_db.flush.assert_awaited_once()
        mock_db.refresh.assert_awaited_once()

    async def test_create_minimal(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """create works with minimal required fields."""
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.create(
            user_id=self.USER_ID,
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            content="Test fact",
        )

        assert result is not None

    # ── create_or_skip ─────────────────────────────────────────────────────────

    async def test_create_or_skip_creates(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """create_or_skip creates a fact when no conflict."""
        fact = self._mock_fact()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = fact
        mock_db.execute.return_value = mock_result

        result = await repo.create_or_skip(
            user_id=self.USER_ID,
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            content="Alice likes hiking",
            subject="Alice",
            predicate="likes",
            obj="hiking",
        )

        assert result == fact
        mock_db.flush.assert_awaited_once()

    async def test_create_or_skip_skips_on_conflict(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """create_or_skip returns None when conflict fires."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.create_or_skip(
            user_id=self.USER_ID,
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            content="Duplicate",
        )

        assert result is None
        mock_db.flush.assert_awaited_once()

    # ── batch_create ───────────────────────────────────────────────────────────

    async def test_batch_create(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """batch_create inserts multiple facts."""
        facts = [
            {"subject": "Alice", "predicate": "likes", "object": "hiking"},
            {"subject": "Bob", "predicate": "works_at", "object": "Acme"},
        ]
        created = [self._mock_fact(), self._mock_fact(id=uuid4())]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = created
        mock_db.execute.return_value = mock_result
        mock_db.refresh.return_value = None

        result = await repo.batch_create(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            user_id=self.USER_ID,
            facts=facts,
        )

        assert len(result) == 2
        mock_db.execute.assert_awaited_once()

    async def test_batch_create_empty(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """batch_create returns empty list when no facts."""
        result = await repo.batch_create(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            user_id=self.USER_ID,
            facts=[],
        )

        assert result == []

    async def test_batch_create_with_skip_conflict(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """batch_create with on_conflict='skip' uses ON CONFLICT DO NOTHING."""
        facts = [{"subject": "A", "predicate": "is", "object": "B"}]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result
        mock_db.refresh.return_value = None

        result = await repo.batch_create(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            user_id=self.USER_ID,
            facts=facts,
            on_conflict="skip",
        )

        assert result == []

    # ── batch_create_or_skip ───────────────────────────────────────────────────

    async def test_batch_create_or_skip(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """batch_create_or_skip inserts facts with ON CONFLICT DO NOTHING."""
        facts = [
            {"subject": "A", "predicate": "is", "object": "B", "confidence": 0.9},
        ]
        created = [self._mock_fact()]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = created
        mock_db.execute.return_value = mock_result
        mock_db.refresh.return_value = None

        result = await repo.batch_create_or_skip(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            user_id=self.USER_ID,
            source_episode_id=self.EPISODE_ID,
            facts=facts,
        )

        assert len(result) == 1

    async def test_batch_create_or_skip_honors_explicit_content(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """M1 — explicit content is kept, never rebuilt from the SPO join."""
        facts = [
            {
                "subject": "A",
                "predicate": "is",
                "object": "B",
                "confidence": 0.9,
                "content": "Custom statement",
            },
        ]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [self._mock_fact()]
        mock_db.execute.return_value = mock_result

        await repo.batch_create_or_skip(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            user_id=self.USER_ID,
            source_episode_id=self.EPISODE_ID,
            facts=facts,
        )

        (_, rows) = mock_db.execute.await_args.args
        assert rows[0]["content"] == "Custom statement"

    async def test_batch_create_or_skip_defaults_content_to_spo_join(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """M1 — missing content falls back to the ``subject predicate object`` join."""
        facts = [
            {"subject": "A", "predicate": "is", "object": "B", "confidence": 0.9},
        ]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [self._mock_fact()]
        mock_db.execute.return_value = mock_result

        await repo.batch_create_or_skip(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            user_id=self.USER_ID,
            source_episode_id=self.EPISODE_ID,
            facts=facts,
        )

        (_, rows) = mock_db.execute.await_args.args
        assert rows[0]["content"] == "A is B"

    async def test_batch_create_or_skip_honors_caller_valid_from(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """M1 — caller-provided ``valid_from`` wins over the repo's own clock."""
        caller_now = datetime(2026, 5, 1, 9, 30, tzinfo=timezone.utc)
        facts = [
            {
                "subject": "A",
                "predicate": "is",
                "object": "B",
                "confidence": 0.9,
                "valid_from": caller_now,
            },
        ]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [self._mock_fact()]
        mock_db.execute.return_value = mock_result

        await repo.batch_create_or_skip(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            user_id=self.USER_ID,
            source_episode_id=self.EPISODE_ID,
            facts=facts,
        )

        (_, rows) = mock_db.execute.await_args.args
        assert rows[0]["valid_from"] == caller_now

    async def test_batch_create_or_skip_defaults_valid_from_to_now(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """M1 — without ``valid_from`` the repo stamps the current UTC instant."""
        facts = [
            {"subject": "A", "predicate": "is", "object": "B", "confidence": 0.9},
        ]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [self._mock_fact()]
        mock_db.execute.return_value = mock_result

        await repo.batch_create_or_skip(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            user_id=self.USER_ID,
            source_episode_id=self.EPISODE_ID,
            facts=facts,
        )

        (_, rows) = mock_db.execute.await_args.args
        assert rows[0]["valid_from"].tzinfo == timezone.utc

    # ── get_all_active_for_project ─────────────────────────────────────────────

    async def test_get_all_active_for_project(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """get_all_active_for_project returns non-invalidated facts."""
        facts = [self._mock_fact(), self._mock_fact()]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = facts
        mock_db.execute.return_value = mock_result

        result = await repo.get_all_active_for_project(
            project_id=self.PROJECT_ID
        )

        assert result == facts

    async def test_get_all_active_for_project_empty(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """get_all_active_for_project returns empty list."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        result = await repo.get_all_active_for_project(
            project_id=self.PROJECT_ID
        )

        assert result == []

    # ── get_facts_at_time ──────────────────────────────────────────────────────

    async def test_get_facts_at_time(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """get_facts_at_time returns facts valid at a given timestamp."""
        facts = [self._mock_fact()]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = facts
        mock_db.execute.return_value = mock_result

        timestamp = datetime.now(timezone.utc)
        result = await repo.get_facts_at_time(
            project_id=self.PROJECT_ID, timestamp=timestamp
        )

        assert result == facts

    async def test_get_facts_at_time_with_pagination(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """get_facts_at_time supports limit and offset."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        result = await repo.get_facts_at_time(
            project_id=self.PROJECT_ID,
            timestamp=datetime.now(timezone.utc),
            limit=10,
            offset=5,
        )

        assert result == []

    # ── get_facts_in_range ─────────────────────────────────────────────────────

    async def test_get_facts_in_range(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """get_facts_in_range returns facts overlapping a range."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        start = datetime(2024, 1, 1)
        end = datetime(2024, 12, 31)
        result = await repo.get_facts_in_range(
            project_id=self.PROJECT_ID, start=start, end=end
        )

        assert result == []

    # ── soft_delete_by_project ─────────────────────────────────────────────────

    async def test_soft_delete_by_project(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """soft_delete_by_project marks facts as invalid."""
        mock_result = MagicMock()
        mock_result.rowcount = 10
        mock_db.execute.return_value = mock_result

        count = await repo.soft_delete_by_project(project_id=self.PROJECT_ID)

        assert count == 10
        mock_db.flush.assert_awaited_once()

    async def test_soft_delete_by_project_zero(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """soft_delete_by_project returns 0 when no facts match."""
        mock_result = MagicMock()
        mock_result.rowcount = 0
        mock_db.execute.return_value = mock_result

        count = await repo.soft_delete_by_project(project_id=self.PROJECT_ID)

        assert count == 0

    # ── list_by_session ────────────────────────────────────────────────────────

    async def test_list_by_session(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """list_by_session returns paginated facts for a session."""
        mock_result = MagicMock()
        mock_result.fetchall.return_value = [
            (
                str(self.FACT_ID), "Alice likes hiking", "Alice", "likes",
                "hiking", 0.95, str(self.EPISODE_ID),
                datetime.now(timezone.utc), "literal", "literal",
                None, None, None, None, None,
            ),
        ]
        mock_db.execute.return_value = mock_result

        facts, cursor = await repo.list_by_session(
            organization_id=self.ORG_ID,
            session_id=self.SESSION_ID,
            limit=10,
        )

        assert len(facts) == 1
        assert facts[0]["subject"] == "Alice"

    async def test_list_by_session_with_cursor(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """list_by_session decodes cursor and filters."""
        mock_result = MagicMock()
        mock_result.fetchall.return_value = []
        mock_db.execute.return_value = mock_result

        facts, cursor = await repo.list_by_session(
            organization_id=self.ORG_ID,
            session_id=self.SESSION_ID,
            limit=10,
            cursor="some-cursor",
        )

        assert facts == []
        assert cursor is None

    async def test_list_by_session_empty(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """list_by_session returns empty when no facts."""
        mock_result = MagicMock()
        mock_result.fetchall.return_value = []
        mock_db.execute.return_value = mock_result

        facts, cursor = await repo.list_by_session(
            organization_id=self.ORG_ID,
            session_id=self.SESSION_ID,
            limit=10,
        )

        assert facts == []

    # ── search_by_vector ───────────────────────────────────────────────────────

    async def test_search_by_vector(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """search_by_vector returns ranked fact results."""
        mock_result = MagicMock()
        mock_result.fetchall.return_value = [
            (str(self.FACT_ID), "Alice likes hiking", "Alice", "likes", "hiking", 0.95, 0.92),
        ]
        mock_db.execute.return_value = mock_result

        results = await repo.search_by_vector(
            embedding=[0.1, 0.2, 0.3],
            project_id=self.PROJECT_ID,
            limit=10,
        )

        assert len(results) == 1
        assert results[0]["score"] == 0.92

    async def test_search_by_vector_empty(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """search_by_vector returns empty when no matches."""
        mock_result = MagicMock()
        mock_result.fetchall.return_value = []
        mock_db.execute.return_value = mock_result

        results = await repo.search_by_vector(
            embedding=[0.1, 0.2, 0.3],
            project_id=self.PROJECT_ID,
            limit=10,
        )

        assert results == []

    # ── search_by_bm25 ─────────────────────────────────────────────────────────

    async def test_search_by_bm25(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """search_by_bm25 returns full-text ranked results."""
        mock_result = MagicMock()
        mock_result.fetchall.return_value = [
            (
                str(self.FACT_ID), "Alice likes hiking", "Alice", "likes",
                "hiking", 0.95, 0.85, None, None, None,
            ),
        ]
        mock_db.execute.return_value = mock_result

        results = await repo.search_by_bm25(
            query="hiking",
            project_id=self.PROJECT_ID,
            org_id=self.ORG_ID,
            limit=10,
        )

        assert len(results) == 1
        assert results[0]["score"] == 0.85

    async def test_search_by_bm25_empty(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """search_by_bm25 returns empty when no matches."""
        mock_result = MagicMock()
        mock_result.fetchall.return_value = []
        mock_db.execute.return_value = mock_result

        results = await repo.search_by_bm25(
            query="nonexistent",
            project_id=self.PROJECT_ID,
            org_id=self.ORG_ID,
            limit=10,
        )

        assert results == []

    # ── get_by_id ─────────────────────────────────────────────────────────────

    async def test_get_by_id(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_id returns the matching fact."""
        fact = self._mock_fact()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = fact
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_id(self.FACT_ID)

        assert result == fact
        mock_db.execute.assert_awaited_once()

    async def test_get_by_id_scopes_to_organization(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_id passes the tenant filter when provided (defense-in-depth)."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_id(
            self.FACT_ID, organization_id=self.ORG_ID
        )

        assert result is None
        mock_db.execute.assert_awaited_once()

    # ── record_invalidation_event ─────────────────────────────────────────────

    async def test_record_invalidation_event(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """record_invalidation_event inserts a lineage row and flushes."""
        at_time = datetime.now(UTC)
        mock_db.flush.return_value = None

        await repo.record_invalidation_event(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            old_fact_id=self.FACT_ID,
            new_fact_id=None,
            kind="retracted",
            reason="user correction",
            at_time=at_time,
        )

        mock_db.add.assert_called_once()
        event = mock_db.add.call_args.args[0]
        assert event.organization_id == self.ORG_ID
        assert event.project_id == self.PROJECT_ID
        assert event.old_fact_id == self.FACT_ID
        assert event.new_fact_id is None
        assert event.kind == "retracted"
        assert event.reason == "user correction"
        assert event.at_time == at_time
        mock_db.flush.assert_awaited_once()

    # ── batch temporal guards ─────────────────────────────────────────────────

    async def test_batch_create_accepts_valid_to(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """batch_create carries a caller-provided ``valid_to`` into the row."""
        valid_to = datetime(2026, 12, 31, tzinfo=UTC)
        facts = [
            {
                "subject": "A",
                "predicate": "is",
                "object": "B",
                "valid_from": datetime(2026, 1, 1, tzinfo=UTC),
                "valid_to": valid_to,
            },
        ]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [self._mock_fact()]
        mock_db.execute.return_value = mock_result
        mock_db.refresh.return_value = None

        await repo.batch_create(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            user_id=self.USER_ID,
            facts=facts,
        )

        (_, rows) = mock_db.execute.await_args.args
        assert rows[0]["valid_to"] == valid_to

    async def test_batch_create_rejects_born_dead_range(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """batch_create raises ValidationError when valid_from >= valid_to."""
        facts = [
            {
                "subject": "A",
                "predicate": "is",
                "object": "B",
                "valid_from": datetime(2026, 5, 1, tzinfo=UTC),
                "valid_to": datetime(2026, 4, 1, tzinfo=UTC),
            },
        ]

        with pytest.raises(ValidationError):
            await repo.batch_create(
                organization_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                user_id=self.USER_ID,
                facts=facts,
            )

        mock_db.execute.assert_not_awaited()

    async def test_batch_create_rejects_past_valid_to_without_valid_from(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """batch_create rejects a valid_to already in the past when no
        valid_from is given — a missing valid_from defaults to now at
        insert, making the range born-dead."""
        from datetime import timedelta

        facts = [
            {
                "subject": "A",
                "predicate": "is",
                "object": "B",
                "valid_to": datetime.now(UTC) - timedelta(hours=1),
            },
        ]

        with pytest.raises(ValidationError):
            await repo.batch_create(
                organization_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                user_id=self.USER_ID,
                facts=facts,
            )

        mock_db.execute.assert_not_awaited()

    async def test_batch_create_or_skip_accepts_valid_to(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """batch_create_or_skip carries a caller-provided ``valid_to``."""
        valid_to = datetime(2026, 12, 31, tzinfo=UTC)
        facts = [
            {
                "subject": "A",
                "predicate": "is",
                "object": "B",
                "confidence": 0.9,
                "valid_from": datetime(2026, 1, 1, tzinfo=UTC),
                "valid_to": valid_to,
            },
        ]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [self._mock_fact()]
        mock_db.execute.return_value = mock_result

        await repo.batch_create_or_skip(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            user_id=self.USER_ID,
            source_episode_id=self.EPISODE_ID,
            facts=facts,
        )

        (_, rows) = mock_db.execute.await_args.args
        assert rows[0]["valid_to"] == valid_to

    async def test_batch_create_or_skip_rejects_born_dead_range(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """batch_create_or_skip raises ValidationError on born-dead ranges."""
        facts = [
            {
                "subject": "A",
                "predicate": "is",
                "object": "B",
                "confidence": 0.9,
                "valid_from": datetime(2026, 5, 1, tzinfo=UTC),
                "valid_to": datetime(2026, 4, 1, tzinfo=UTC),
            },
        ]

        with pytest.raises(ValidationError):
            await repo.batch_create_or_skip(
                organization_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                user_id=self.USER_ID,
                source_episode_id=self.EPISODE_ID,
                facts=facts,
            )

        mock_db.execute.assert_not_awaited()

    # ── set_superseded_by ─────────────────────────────────────────────────────

    async def test_set_superseded_by_issues_update(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """set_superseded_by issues an UPDATE writing the successor link."""
        mock_db.flush.return_value = None

        await repo.set_superseded_by(self.FACT_ID, self.EPISODE_ID)

        mock_db.execute.assert_awaited_once()
        stmt = mock_db.execute.await_args.args[0]
        assert isinstance(stmt, Update)
        assert stmt.table.name == "facts"
        column_keys = {col.key for col in stmt._values}
        assert "superseded_by_fact_id" in column_keys
        mock_db.flush.assert_awaited_once()

    async def test_set_superseded_by_none_clears_lineage(
        self, repo: FactRepository, mock_db: AsyncMock
    ) -> None:
        """set_superseded_by(None) issues the UPDATE with a NULL successor
        (retraction/expiry path closes the lineage)."""
        mock_db.flush.return_value = None

        await repo.set_superseded_by(self.FACT_ID, None)

        mock_db.execute.assert_awaited_once()
        stmt = mock_db.execute.await_args.args[0]
        assert isinstance(stmt, Update)
        assert stmt.table.name == "facts"
        column_keys = {col.key for col in stmt._values}
        assert "superseded_by_fact_id" in column_keys
        mock_db.flush.assert_awaited_once()
