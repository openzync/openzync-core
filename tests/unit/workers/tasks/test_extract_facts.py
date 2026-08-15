"""Unit tests for the fact-extraction post-processing helpers.

The standalone ``extract_facts`` ARQ task was retired in favour of the
combined ``enrich_episode`` worker (which calls ``process_facts_output``
as its facts section).  These tests exercise ``_filter_facts`` (pure) and
``process_facts_output`` directly.  Entity-resolution matching is covered by
``test_fact_entity_resolution.py``; the caller-owned orchestration (LLM call,
idempotency, episode not found) is covered by ``test_enrich_episode.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from schemas.llm_outputs import FactExtractionOutput
from services.fact_invalidation_service import FactIngestionResult
from workers.tasks.extract_facts import _filter_facts, process_facts_output

_EPISODE_ID = str(uuid4())
_ORG_ID = str(uuid4())
_PROJECT_ID = str(uuid4())
_SESSION_ID = str(uuid4())
_USER_ID = str(uuid4())
_TRACE_ID = "trace-002"


# ── _filter_facts (pure) ───────────────────────────────────────────────────────


class TestFilterFacts:
    """_filter_facts confidence and triple-completeness filtering."""

    def test_low_confidence_filtered(self) -> None:
        """Facts below the confidence threshold are dropped."""
        facts = [
            {"subject": "Alice", "predicate": "works_at", "object": "Acme Corp", "confidence": 0.95},
            {"subject": "Bob", "predicate": "might_work_at", "object": "Unknown", "confidence": 0.2},
        ]
        valid = _filter_facts(facts)
        assert len(valid) == 1
        assert valid[0]["subject"] == "Alice"

    def test_incomplete_triple_filtered(self) -> None:
        """Facts missing subject/predicate/object are dropped."""
        facts = [
            {"subject": "Alice", "predicate": "works_at", "object": "Acme Corp", "confidence": 0.95},
            {"subject": "", "predicate": "is", "object": "Unknown", "confidence": 0.8},
            {"subject": "Bob", "predicate": "  ", "object": "Acme Corp", "confidence": 0.9},
        ]
        valid = _filter_facts(facts)
        assert len(valid) == 1
        assert valid[0]["subject"] == "Alice"

    def test_missing_confidence_defaults_to_half(self) -> None:
        """Facts without a confidence field default to 0.5 (kept)."""
        facts = [{"subject": "Alice", "predicate": "works_at", "object": "Acme Corp"}]
        valid = _filter_facts(facts)
        assert len(valid) == 1
        assert valid[0]["confidence"] == 0.5

    def test_empty_list_returns_empty(self) -> None:
        """No facts in → no facts out."""
        assert _filter_facts([]) == []

    def test_born_dead_window_dropped(self) -> None:
        """A fact whose validity window has already ended (valid_from >=
        valid_to) is dropped, not raised — the repo guard would otherwise
        wedge the worker via with_retry."""
        facts = [
            {
                "subject": "Alice",
                "predicate": "works_at",
                "object": "Acme Corp",
                "confidence": 0.95,
                "valid_from": datetime(2026, 5, 1, tzinfo=UTC),
                "valid_to": datetime(2026, 4, 1, tzinfo=UTC),  # born-dead
            },
            {
                "subject": "Bob",
                "predicate": "leads",
                "object": "Acme Corp",
                "confidence": 0.9,
                "valid_from": datetime(2026, 1, 1, tzinfo=UTC),
                "valid_to": datetime(2026, 12, 31, tzinfo=UTC),  # valid window
            },
        ]

        valid = _filter_facts(facts)

        assert len(valid) == 1
        assert valid[0]["subject"] == "Bob"
        assert valid[0]["valid_to"] == datetime(2026, 12, 31, tzinfo=UTC)

    def test_zero_length_window_dropped(self) -> None:
        """valid_from == valid_to is a zero-length window — also born-dead."""
        facts = [
            {
                "subject": "Alice",
                "predicate": "works_at",
                "object": "Acme Corp",
                "confidence": 0.95,
                "valid_from": datetime(2026, 5, 1, tzinfo=UTC),
                "valid_to": datetime(2026, 5, 1, tzinfo=UTC),
            },
        ]

        valid = _filter_facts(facts)

        assert valid == []

    def test_open_ended_window_kept(self) -> None:
        """A future-closing window (valid_from < valid_to) survives; a
        window with only valid_from (no valid_to) survives too."""
        facts = [
            {
                "subject": "Alice",
                "predicate": "works_at",
                "object": "Acme Corp",
                "confidence": 0.95,
                "valid_from": datetime(2026, 1, 1, tzinfo=UTC),
            },
            {
                "subject": "Bob",
                "predicate": "leads",
                "object": "Acme Corp",
                "confidence": 0.9,
                "valid_to": datetime(2026, 12, 31, tzinfo=UTC),
            },
        ]

        valid = _filter_facts(facts)

        assert len(valid) == 2


# ── process_facts_output ───────────────────────────────────────────────────────


@pytest.mark.unit
class TestProcessFactsOutput:
    """process_facts_output persistence via fact supersession."""

    @pytest.fixture
    def fact_repo(self) -> MagicMock:
        return MagicMock()

    @pytest.fixture
    def episode_repo(self) -> MagicMock:
        repo = MagicMock()
        repo.apply_enrichment_bits = AsyncMock()
        return repo

    @pytest.fixture
    def db(self) -> AsyncMock:
        m = AsyncMock()
        m.flush = AsyncMock()
        return m

    def _parsed(self, facts: list[dict] | None = None) -> FactExtractionOutput:
        return FactExtractionOutput(facts=facts or [])

    def _persisted_fact(self) -> MagicMock:
        fact = MagicMock()
        fact.id = uuid4()
        fact.content = "Alice works_at Acme Corp"
        return fact

    @pytest.mark.asyncio
    async def test_empty_facts_no_persistence(
        self,
        db: AsyncMock,
        fact_repo: MagicMock,
        episode_repo: MagicMock,
    ) -> None:
        """No facts extracted → nothing persisted, no invalidation run."""
        with patch(
            "services.fact_invalidation_service.FactInvalidationService"
        ) as mock_inval_cls:
            result = await process_facts_output(
                db=db,
                graph_backend=MagicMock(),
                entity_repo=MagicMock(),
                fact_repo=fact_repo,
                episode_repo=episode_repo,
                org_id=_ORG_ID,
                episode_id=_EPISODE_ID,
                project_id=_PROJECT_ID,
                session_id=_SESSION_ID,
                user_id=_USER_ID,
                trace_id=_TRACE_ID,
                parsed=self._parsed([]),
                known_entities=[],
                existing_facts=[],
            )

        assert result == []
        mock_inval_cls.assert_not_called()
        episode_repo.apply_enrichment_bits.assert_not_called()

    @pytest.mark.asyncio
    async def test_all_facts_filtered_no_persistence(
        self,
        db: AsyncMock,
        fact_repo: MagicMock,
        episode_repo: MagicMock,
    ) -> None:
        """Only sub-threshold facts → nothing persisted."""
        with patch(
            "services.fact_invalidation_service.FactInvalidationService"
        ) as mock_inval_cls:
            result = await process_facts_output(
                db=db,
                graph_backend=MagicMock(),
                entity_repo=MagicMock(),
                fact_repo=fact_repo,
                episode_repo=episode_repo,
                org_id=_ORG_ID,
                episode_id=_EPISODE_ID,
                project_id=_PROJECT_ID,
                session_id=_SESSION_ID,
                user_id=_USER_ID,
                trace_id=_TRACE_ID,
                parsed=self._parsed([
                    {"subject": "Bob", "predicate": "might_work_at",
                     "object": "Unknown", "confidence": 0.1},
                ]),
                known_entities=[],
                existing_facts=[],
            )

        assert result == []
        mock_inval_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_success_persists_via_supersession(
        self,
        db: AsyncMock,
        fact_repo: MagicMock,
        episode_repo: MagicMock,
    ) -> None:
        """Valid facts flow through supersession, bit set, ids returned."""
        persisted = self._persisted_fact()

        with (
            patch("services.fact_invalidation_service.FactInvalidationService") as mock_inval_cls,
            patch("services.graph_edge_sync_service.GraphEdgeSyncService"),
            patch("services.cache_service.CacheService"),
        ):
            mock_inval = MagicMock()
            mock_inval.ingest_with_supersession = AsyncMock(
                return_value=FactIngestionResult(created=[persisted], superseded_count=0)
            )
            mock_inval_cls.return_value = mock_inval

            entity_repo = MagicMock()
            entity_repo.upsert_relationship = AsyncMock(return_value={"id": str(uuid4())})
            entity_repo.get_entity_by_name = AsyncMock(return_value=None)

            result = await process_facts_output(
                db=db,
                graph_backend=MagicMock(),
                entity_repo=entity_repo,
                fact_repo=fact_repo,
                episode_repo=episode_repo,
                org_id=_ORG_ID,
                episode_id=_EPISODE_ID,
                project_id=_PROJECT_ID,
                session_id=_SESSION_ID,
                user_id=_USER_ID,
                trace_id=_TRACE_ID,
                parsed=self._parsed([
                    {"subject": "Alice", "predicate": "works_at",
                     "object": "Acme Corp", "confidence": 0.95,
                     "subject_type": "literal", "object_type": "literal"},
                ]),
                known_entities=[],
                existing_facts=[],
            )

        assert result == [str(persisted.id)]
        mock_inval.ingest_with_supersession.assert_awaited_once()
        episode_repo.apply_enrichment_bits.assert_awaited_once_with(
            UUID(_EPISODE_ID),
            __import__("workers.tasks.base", fromlist=["ENRICHMENT_FACTS"]).ENRICHMENT_FACTS,
        )
        db.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_graph_disabled_facts_still_persist(
        self,
        db: AsyncMock,
        fact_repo: MagicMock,
        episode_repo: MagicMock,
    ) -> None:
        """graph_backend=None → facts persist in PG, no graph ops."""
        persisted = self._persisted_fact()

        with (
            patch("services.fact_invalidation_service.FactInvalidationService") as mock_inval_cls,
            patch("services.graph_edge_sync_service.GraphEdgeSyncService") as mock_sync_cls,
            patch("services.cache_service.CacheService"),
        ):
            mock_inval = MagicMock()
            mock_inval.ingest_with_supersession = AsyncMock(
                return_value=FactIngestionResult(created=[persisted], superseded_count=0)
            )
            mock_inval_cls.return_value = mock_inval

            result = await process_facts_output(
                db=db,
                graph_backend=None,
                entity_repo=None,
                fact_repo=fact_repo,
                episode_repo=episode_repo,
                org_id=_ORG_ID,
                episode_id=_EPISODE_ID,
                project_id=_PROJECT_ID,
                session_id=_SESSION_ID,
                user_id=_USER_ID,
                trace_id=_TRACE_ID,
                parsed=self._parsed([
                    {"subject": "Alice", "predicate": "works_at",
                     "object": "Acme Corp", "confidence": 0.95,
                     "subject_type": "literal", "object_type": "literal"},
                ]),
                known_entities=[],
                existing_facts=[],
            )

        assert result == [str(persisted.id)]
        # Supersession ingest runs (Postgres persistence is the primary path).
        mock_inval.ingest_with_supersession.assert_awaited_once()
        # No graph sync service is constructed for a disabled backend.
        mock_sync_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_embed_fact_enqueued_when_redis_provided(
        self,
        db: AsyncMock,
        fact_repo: MagicMock,
        episode_repo: MagicMock,
    ) -> None:
        """arq_redis present → one embed_fact job per persisted fact."""
        persisted = self._persisted_fact()

        with (
            patch("services.fact_invalidation_service.FactInvalidationService") as mock_inval_cls,
            patch("services.graph_edge_sync_service.GraphEdgeSyncService"),
            patch("services.cache_service.CacheService"),
            patch("services.worker.worker_settings.get_queue_name",
                  return_value="OpenZync:test:queue:high"),
        ):
            mock_inval = MagicMock()
            mock_inval.ingest_with_supersession = AsyncMock(
                return_value=FactIngestionResult(created=[persisted], superseded_count=0)
            )
            mock_inval_cls.return_value = mock_inval

            arq_redis = AsyncMock()

            entity_repo = MagicMock()
            entity_repo.get_entity_by_name = AsyncMock(return_value=None)
            entity_repo.upsert_relationship = AsyncMock(return_value={"id": str(uuid4())})

            # Seed the WorkerSettings singleton so the lazy ``w_settings.ENV``
            # read inside ``process_facts_output`` succeeds (same pattern as
            # ``_seed_worker_settings`` in test_worker.py).
            import services.worker.worker_settings as _ws
            from services.worker.worker_settings import WorkerSettings

            _ws._settings = WorkerSettings(
                DATABASE_URL="postgresql+asyncpg://localhost:5432/test",
                REDIS_URL="redis://localhost:6379/0",
                ENV="test",
            )

            await process_facts_output(
                db=db,
                graph_backend=MagicMock(),
                entity_repo=entity_repo,
                fact_repo=fact_repo,
                episode_repo=episode_repo,
                org_id=_ORG_ID,
                episode_id=_EPISODE_ID,
                project_id=_PROJECT_ID,
                session_id=_SESSION_ID,
                user_id=_USER_ID,
                trace_id=_TRACE_ID,
                parsed=self._parsed([
                    {"subject": "Alice", "predicate": "works_at",
                     "object": "Acme Corp", "confidence": 0.95,
                     "subject_type": "literal", "object_type": "literal"},
                ]),
                known_entities=[],
                existing_facts=[],
                arq_redis=arq_redis,
            )

        arq_redis.enqueue_job.assert_awaited_once()
        job_args = arq_redis.enqueue_job.await_args
        assert job_args.args[0] == "embed_fact"
        assert job_args.kwargs["fact_id"] == str(persisted.id)

    @pytest.mark.asyncio
    async def test_slot_map_keys_on_content_fallback_keeps_successor_linkage(
        self,
        db: AsyncMock,
        fact_repo: MagicMock,
        episode_repo: MagicMock,
    ) -> None:
        """Slot map resolves via the service's content fallback.

        ``content_to_fact`` keys on ``content or "{subject} {predicate}
        {object}"`` — the exact fallback ``FactInvalidationService._prepare_entry``
        computes when persisting — so a persisted row's content (here the
        SPO join, since ``FactOutput`` carries no explicit ``content`` field)
        always finds its input slot and the successor linkage survives.
        """
        persisted = self._persisted_fact()  # content = "Alice works_at Acme Corp"

        with (
            patch(
                "services.fact_invalidation_service.FactInvalidationService"
            ) as mock_inval_cls,
            patch("services.graph_edge_sync_service.GraphEdgeSyncService"),
            patch("services.cache_service.CacheService"),
        ):
            mock_inval = MagicMock()
            mock_inval.ingest_with_supersession = AsyncMock(
                return_value=FactIngestionResult(
                    created=[persisted], superseded_count=0
                )
            )
            mock_inval_cls.return_value = mock_inval

            entity_repo = MagicMock()
            entity_repo.upsert_relationship = AsyncMock(
                return_value={"id": str(uuid4())}
            )
            entity_repo.get_entity_by_name = AsyncMock(return_value=None)

            new_facts, slot_map = await process_facts_output(
                db=db,
                graph_backend=MagicMock(),
                entity_repo=entity_repo,
                fact_repo=fact_repo,
                episode_repo=episode_repo,
                org_id=_ORG_ID,
                episode_id=_EPISODE_ID,
                project_id=_PROJECT_ID,
                session_id=_SESSION_ID,
                user_id=_USER_ID,
                trace_id=_TRACE_ID,
                parsed=self._parsed([
                    {"subject": "Alice", "predicate": "works_at",
                     "object": "Acme Corp", "confidence": 0.95,
                     "subject_type": "literal", "object_type": "literal"},
                ]),
                known_entities=[],
                existing_facts=[],
                return_slot_map=True,
            )

        # Slot 1 (the only parsed fact) resolves to the persisted row — the
        # successor link for an "N1" LLM invalidation reference is not lost.
        assert new_facts == [persisted]
        assert slot_map == {1: persisted}
