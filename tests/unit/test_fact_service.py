"""Unit tests for FactService — business logic with mocked dependencies."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from core.exceptions import GraphBackendUnavailableError, NotFoundError
from packages.graph_backend.postgres import PostgresGraphBackend
from schemas.facts import FactTriple
from services.fact_invalidation_service import (
    FactInvalidationService as RealInvalidationService,
)
from services.fact_service import FactService


@pytest.mark.unit
class TestFactService:
    """FactService unit tests."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")
    USER_ID = UUID("00000000-0000-0000-0000-000000000002")
    SESSION_ID = UUID("00000000-0000-0000-0000-000000000010")
    FACT_1_ID = UUID("00000000-0000-0000-0000-000000000100")
    FACT_2_ID = UUID("00000000-0000-0000-0000-000000000101")

    @pytest.fixture
    def service(self) -> FactService:
        mock_db = AsyncMock(spec=AsyncSession)
        mock_redis = AsyncMock()
        mock_redis.get.return_value = None  # no cached idempotency
        mock_fact_repo = AsyncMock()
        mock_fact_repo.create.return_value = MagicMock(id=UUID("00000000-0000-0000-0000-000000000099"))
        mock_session_repo = AsyncMock()

        s = FactService(
            db=mock_db,
            redis_client=mock_redis,
            fact_repo=mock_fact_repo,
            session_repo=mock_session_repo,
        )
        return s

    def _sample_triple(self, **kwargs) -> FactTriple:
        return FactTriple(
            subject=kwargs.get("subject", "Python"),
            predicate=kwargs.get("predicate", "is"),
            object=kwargs.get("object", "great"),
            content=kwargs.get("content", "Python is great"),
            confidence=kwargs.get("confidence", 0.95),
        )

    @pytest.mark.asyncio
    async def test_ingest_facts_empty_list_returns_accepted(self, service: FactService) -> None:
        """Ingesting an empty fact list returns accepted (schema-level validation
        catches empty lists before reaching the service)."""
        result = await service.ingest_facts(
            org_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            created_by=self.USER_ID,
            facts=[],
            session_external_id="session-abc",
        )
        assert result.status == "accepted"

    @pytest.mark.asyncio
    async def test_ingest_facts_happy_path(self, service: FactService) -> None:
        """Verify successful fact ingestion with 2 sample triples and a session."""
        # Arrange
        mock_session = MagicMock(id=self.SESSION_ID)
        service._session_repo.get_by_external_id.return_value = mock_session

        mock_fact_1 = MagicMock(id=self.FACT_1_ID)
        mock_fact_2 = MagicMock(id=self.FACT_2_ID)
        service._fact_repo.batch_create.return_value = [mock_fact_1, mock_fact_2]

        mock_arq_pool = AsyncMock()
        facts = [
            self._sample_triple(subject="Alice", predicate="likes", object="hiking"),
            self._sample_triple(
                subject="Bob", predicate="works_at", object="AcmeCorp"
            ),
        ]

        with patch("services.fact_service.get_arq", return_value=mock_arq_pool):
            result = await service.ingest_facts(
                org_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                created_by=self.USER_ID,
                facts=facts,
                session_external_id="session-abc",
            )

        # Assert
        assert result.status == "accepted"
        assert isinstance(result.job_id, str)
        assert len(result.job_id) > 0
        assert result.accepted_count == 2
        assert "accepted" in result.message.lower()

        service._fact_repo.batch_create.assert_awaited_once()
        service._session_repo.get_by_external_id.assert_awaited_once_with(
            org_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            external_id="session-abc",
        )

    @pytest.mark.asyncio
    async def test_ingest_facts_session_not_found(self, service: FactService) -> None:
        """Verify NotFoundError is raised when session_external_id doesn't match."""
        service._session_repo.get_by_external_id.return_value = None

        facts = [self._sample_triple()]

        with pytest.raises(NotFoundError) as exc_info:
            await service.ingest_facts(
                org_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                created_by=self.USER_ID,
                facts=facts,
                session_external_id="nonexistent-session",
            )

        assert "Session" in exc_info.value.message
        assert "not found" in exc_info.value.message
        service._session_repo.get_by_external_id.assert_awaited_once()
        service._fact_repo.batch_create.assert_not_awaited()

    # ── _compute_batch_hash regression tests ─────────────────────────────
    # Intentional change: content is now part of the batch identity. Two
    # batches with the same SPO set but different content MUST hash
    # differently, otherwise the Redis dedup short-circuit silently drops
    # newer content before supersession can run.

    def _hash(self, *triples: FactTriple) -> str:
        return FactService._compute_batch_hash(self.PROJECT_ID, list(triples))

    def test_batch_hash_identical_for_identical_batches(self) -> None:
        """Same SPO + same content yields the same hash."""
        facts = [
            self._sample_triple(subject="Alice", predicate="likes", object="hiking"),
            self._sample_triple(subject="Bob", predicate="works_at", object="AcmeCorp"),
        ]
        assert self._hash(*facts) == self._hash(*facts)

    def test_batch_hash_differs_for_same_spo_different_content(self) -> None:
        """Same SPO but different content must yield a different hash."""
        base = self._sample_triple(subject="Alice", predicate="likes", object="hiking")
        same_spo_different_content = self._sample_triple(
            subject="Alice",
            predicate="likes",
            object="hiking",
            content="Alice absolutely loves hiking every weekend",
        )
        assert self._hash(base) != self._hash(same_spo_different_content)

    def test_batch_hash_ignores_surrounding_whitespace_in_content(self) -> None:
        """Content is stripped before hashing — whitespace-only differences
        are not part of the identity."""
        trimmed = self._sample_triple(content="Python is great")
        padded = self._sample_triple(content="   Python is great   ")
        assert self._hash(trimmed) == self._hash(padded)


# ── Graph edge sync wiring (Phase 3) ──────────────────────────────────────────


class TestFactServiceGraphSync:
    """ingest_facts wires the resolved graph backend into edge expiry.

    The resolver callable (provided by ``get_fact_service``) resolves the
    org's backend lazily; on success a ``GraphEdgeSyncService`` is passed
    to ``FactInvalidationService`` so supersessions expire graph edges.
    Resolution failure must never fail the ingest.
    """

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")
    USER_ID = UUID("00000000-0000-0000-0000-000000000002")
    SRC_ENTITY = UUID("00000000-0000-0000-0000-00000000aaaa")
    TGT_ENTITY = UUID("00000000-0000-0000-0000-00000000bbbb")
    OLD_FACT_ID = UUID("00000000-0000-0000-0000-000000000100")
    NEW_FACT_ID = UUID("00000000-0000-0000-0000-000000000101")

    def _make_backend(self) -> PostgresGraphBackend:
        backend = PostgresGraphBackend(db=AsyncMock(spec=AsyncSession))
        backend.expire_relationships_matching = AsyncMock(return_value=1)
        return backend

    def _base_service(
        self,
        mock_db: AsyncMock,
        mock_redis: AsyncMock,
        mock_fact_repo: AsyncMock,
        resolver: AsyncMock,
    ) -> FactService:
        return FactService(
            db=mock_db,
            redis_client=mock_redis,
            fact_repo=mock_fact_repo,
            session_repo=AsyncMock(),
            graph_backend_resolver=resolver,
        )

    def _fact(self, **overrides) -> SimpleNamespace:
        return SimpleNamespace(
            id=overrides.get("id", self.OLD_FACT_ID),
            subject=overrides.get("subject", "Alice"),
            predicate=overrides.get("predicate", "likes"),
            object=overrides.get("object", "hiking"),
            content=overrides.get("content", "Alice likes hiking"),
            subject_entity_id=overrides.get("subject_entity_id"),
            object_entity_id=overrides.get("object_entity_id"),
        )

    @staticmethod
    async def _commit(
        invalidation: RealInvalidationService, mock_db: AsyncMock
    ) -> None:
        """Fire the queued post-commit effects (mirrors the session commit)."""
        invalidation._on_after_commit(mock_db.sync_session)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    @staticmethod
    def _sample_triple() -> FactTriple:
        return FactTriple(
            subject="Python",
            predicate="is",
            object="great",
            content="Python is great",
            confidence=0.95,
        )

    @pytest.mark.asyncio
    async def test_ingest_supersession_wires_graph_sync_and_keeps_contract(
        self,
    ) -> None:
        """A supersession through ingest_facts queues the edge-sync effect.

        The resolver is invoked with the org ID, the invalidation service
        is constructed with the resolved backend, and the response
        contract is unchanged (202/status, job_id, accepted_count,
        superseded_count).  Literal API facts carry no entity IDs, so the
        D1 rule correctly expires nothing.
        """
        backend = self._make_backend()
        resolver = AsyncMock(return_value=backend)

        mock_db = AsyncMock(spec=AsyncSession)
        mock_db.sync_session = Session()
        mock_redis = AsyncMock()
        mock_redis.get.return_value = None
        mock_redis.scan.return_value = (0, [])
        mock_redis.delete.return_value = 0

        candidate = self._fact(content="Alice likes hiking")  # literal
        mock_fact_repo = AsyncMock()
        mock_fact_repo.find_conflicting_active_for_update.return_value = [candidate]
        mock_fact_repo.batch_create.return_value = [
            self._fact(
                id=self.NEW_FACT_ID,
                content="Alice loves hiking",
                subject_entity_id=None,
                object_entity_id=None,
            )
        ]

        service = self._base_service(mock_db, mock_redis, mock_fact_repo, resolver)
        captured: list[RealInvalidationService] = []

        class _Recording(RealInvalidationService):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                captured.append(self)

        with (
            patch(
                "services.fact_invalidation_service.FactInvalidationService",
                _Recording,
            ),
            patch("services.fact_service.get_arq", return_value=AsyncMock()),
        ):
            result = await service.ingest_facts(
                org_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                created_by=self.USER_ID,
                facts=[
                    FactTriple(
                        subject="Alice",
                        predicate="likes",
                        object="hiking",
                        content="Alice loves hiking",
                    )
                ],
                session_external_id="session-abc",
            )

        # Resolver consulted with the org; backend bound into graph_sync.
        resolver.assert_awaited_once_with(self.ORG_ID)
        assert len(captured) == 1
        sync = captured[0]._graph_sync
        assert sync is not None
        assert sync.backends == [backend]

        # Response contract unchanged.
        assert result.status == "accepted"
        assert isinstance(result.job_id, str) and len(result.job_id) > 0
        assert result.accepted_count == 1
        assert result.superseded_count == 1

        # Supersession queued the effect; literal facts → D1 expires nothing.
        await self._commit(captured[0], mock_db)
        backend.expire_relationships_matching.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_wired_graph_sync_expires_edge_on_retraction(self) -> None:
        """The graph_sync built by ingest_facts actually expires edges.

        A retraction through the wired invalidation service (the same
        instance ``ingest_facts`` constructed) fires the D1 case-1 expiry
        against the resolved Postgres backend.
        """
        backend = self._make_backend()
        resolver = AsyncMock(return_value=backend)

        mock_db = AsyncMock(spec=AsyncSession)
        mock_db.sync_session = Session()
        mock_redis = AsyncMock()
        mock_redis.get.return_value = None
        mock_redis.scan.return_value = (0, [])
        mock_redis.delete.return_value = 0

        # No conflicts on the initial ingest — nothing queued yet.
        mock_fact_repo = AsyncMock()
        mock_fact_repo.find_conflicting_active_for_update.return_value = []
        mock_fact_repo.batch_create.return_value = [
            self._fact(id=self.NEW_FACT_ID, content="Alice loves hiking")
        ]

        service = self._base_service(mock_db, mock_redis, mock_fact_repo, resolver)
        captured: list[RealInvalidationService] = []

        class _Recording(RealInvalidationService):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                captured.append(self)

        with (
            patch(
                "services.fact_invalidation_service.FactInvalidationService",
                _Recording,
            ),
            patch("services.fact_service.get_arq", return_value=AsyncMock()),
        ):
            await service.ingest_facts(
                org_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                created_by=self.USER_ID,
                facts=[
                    FactTriple(
                        subject="Alice",
                        predicate="works_at",
                        object="Acme",
                        content="Alice works_at Acme",
                    )
                ],
                session_external_id="session-abc",
            )

        invalidation = captured[0]
        old_fact = self._fact(
            id=self.OLD_FACT_ID,
            subject_entity_id=self.SRC_ENTITY,
            object_entity_id=self.TGT_ENTITY,
        )
        from datetime import UTC, datetime

        at_time = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
        invalidation.notify_retraction(
            org_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            old_fact=old_fact,
            at_time=at_time,
        )
        await self._commit(invalidation, mock_db)

        backend.expire_relationships_matching.assert_awaited_once_with(
            org_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            source_id=self.SRC_ENTITY,
            target_id=self.TGT_ENTITY,
            relationship_type="likes",
            at_time=at_time,
        )

    @pytest.mark.asyncio
    async def test_backend_resolution_failure_still_ingests(self) -> None:
        """Resolver failure → warning, no graph_sync, ingest still 202."""
        resolver = AsyncMock(
            side_effect=GraphBackendUnavailableError(
                "SurrealDB connection failed for org 00000000-0000-0000-0000-000000000001"
            )
        )

        mock_db = AsyncMock(spec=AsyncSession)
        mock_redis = AsyncMock()
        mock_redis.get.return_value = None
        mock_fact_repo = AsyncMock()
        mock_fact_repo.batch_create.return_value = [
            self._fact(id=self.NEW_FACT_ID, content="Alice is great")
        ]

        service = self._base_service(mock_db, mock_redis, mock_fact_repo, resolver)
        captured: list[RealInvalidationService] = []

        class _Recording(RealInvalidationService):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                captured.append(self)

        with (
            patch(
                "services.fact_invalidation_service.FactInvalidationService",
                _Recording,
            ),
            patch("services.fact_service.get_arq", return_value=AsyncMock()),
        ):
            result = await service.ingest_facts(
                org_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                created_by=self.USER_ID,
                facts=[self._sample_triple()],
                session_external_id="session-abc",
            )

        assert result.status == "accepted"
        assert result.accepted_count == 1
        assert captured[0]._graph_sync is None

    @pytest.mark.asyncio
    async def test_graph_disabled_resolver_none_ingests_without_sync(self) -> None:
        """Resolver returning None (graph disabled) → no graph_sync, no error."""
        resolver = AsyncMock(return_value=None)

        mock_db = AsyncMock(spec=AsyncSession)
        mock_redis = AsyncMock()
        mock_redis.get.return_value = None
        mock_fact_repo = AsyncMock()
        mock_fact_repo.batch_create.return_value = [
            self._fact(id=self.NEW_FACT_ID, content="Alice is great")
        ]

        service = self._base_service(mock_db, mock_redis, mock_fact_repo, resolver)
        captured: list[RealInvalidationService] = []

        class _Recording(RealInvalidationService):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                captured.append(self)

        with (
            patch(
                "services.fact_invalidation_service.FactInvalidationService",
                _Recording,
            ),
            patch("services.fact_service.get_arq", return_value=AsyncMock()),
        ):
            result = await service.ingest_facts(
                org_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                created_by=self.USER_ID,
                facts=[self._sample_triple()],
                session_external_id="session-abc",
            )

        assert result.status == "accepted"
        assert result.accepted_count == 1
        assert captured[0]._graph_sync is None
