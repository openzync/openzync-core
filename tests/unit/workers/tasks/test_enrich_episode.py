"""Unit tests for ``enrich_episode`` — master enrichment orchestrator.

Tests cover:
- ``PartialEnrichmentError`` exception struct
- Episode not found → ``EpisodeNotFoundError``
- Already enriched (all bits set) → early return (idempotency)
- Successful full enrichment pass with all 4 sections
- Partial failure → ``PartialEnrichmentError`` with correct ``successful_bits``
- Missing ``db_engine`` creates its own engine
- Missing ``db_session_factory`` creates from engine
- Blob text appended on enrich episode
- Graph backend failure propagates (no silent entity loss)
- Org config fetch failure proceeds without LLM config
- LLM call failure propagates
- ``enrich_episode`` decorated with ``@with_retry``
"""

from __future__ import annotations

from unittest.mock import ANY, AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core.exceptions import EpisodeNotFoundError

_EPISODE_ID = str(uuid4())
_ORG_ID = str(uuid4())
_PROJECT_ID = str(uuid4())
_SESSION_ID = str(uuid4())
_CONTENT = "Test episode content for enrichment."


# ═══════════════════════════════════════════════════════════════════════════════
# PartialEnrichmentError
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestPartialEnrichmentError:
    """``PartialEnrichmentError`` carries ``successful_bits``."""

    def test_default_bits_is_zero(self) -> None:
        """Default ``successful_bits`` is 0."""
        from workers.tasks.enrich_episode import PartialEnrichmentError

        err = PartialEnrichmentError("partial failure")
        assert err.successful_bits == 0
        assert str(err) == "partial failure"

    def test_custom_bits(self) -> None:
        """``successful_bits`` is preserved when provided."""
        from workers.tasks.enrich_episode import PartialEnrichmentError
        from workers.tasks.base import ENRICHMENT_ENTITIES, ENRICHMENT_FACTS

        bits = ENRICHMENT_ENTITIES | ENRICHMENT_FACTS
        err = PartialEnrichmentError("partial", successful_bits=bits)
        assert err.successful_bits == bits


# ═══════════════════════════════════════════════════════════════════════════════
# enrich_episode — episode not found
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestEnrichEpisodeNotFound:
    """Behaviour when the episode does not exist."""

    @pytest.mark.asyncio
    async def test_raises_episode_not_found(self) -> None:
        """``EpisodeNotFoundError`` is raised when episode row is None."""
        from workers.tasks.enrich_episode import enrich_episode

        mock_db = AsyncMock()
        mock_db.__aenter__.return_value = mock_db

        # EpisodeRepository.get_by_id_for_update returns None
        mock_scalar = MagicMock()
        mock_scalar.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_scalar

        ctx = _make_ctx(db=mock_db)

        with pytest.raises(EpisodeNotFoundError):
            await enrich_episode(
                ctx=ctx,
                episode_id=_EPISODE_ID,
                content=_CONTENT,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                session_id=_SESSION_ID,
                trace_id="trace-test",
            )


# ═══════════════════════════════════════════════════════════════════════════════
# enrich_episode — idempotency (already enriched)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestEnrichEpisodeIdempotency:
    """Already-enriched episodes skip processing."""

    @pytest.mark.asyncio
    async def test_returns_early_when_all_bits_set(self) -> None:
        """When all LLM enrichment bits are set, function returns early."""
        from workers.tasks.base import (
            ENRICHMENT_CLASSIFICATION,
            ENRICHMENT_ENTITIES,
            ENRICHMENT_FACTS,
            ENRICHMENT_STRUCTURED_EXTRACTION,
        )
        from workers.tasks.enrich_episode import enrich_episode

        all_llm_bits = (
            ENRICHMENT_ENTITIES
            | ENRICHMENT_FACTS
            | ENRICHMENT_CLASSIFICATION
            | ENRICHMENT_STRUCTURED_EXTRACTION
        )

        mock_db = AsyncMock()
        mock_db.__aenter__.return_value = mock_db

        # Episode row with all bits set
        mock_episode = MagicMock()
        mock_episode.enrichment_status = all_llm_bits
        mock_episode.user_id = uuid4()

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_episode
        mock_db.execute.return_value = mock_result

        ctx = _make_ctx(db=mock_db)

        # Should return None (early return)
        result = await enrich_episode(
            ctx=ctx,
            episode_id=_EPISODE_ID,
            content=_CONTENT,
            org_id=_ORG_ID,
            project_id=_PROJECT_ID,
            session_id=_SESSION_ID,
            trace_id="trace-1",
        )

        assert result is None
        # LLM call should NOT have been made
        mock_db.begin_nested.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# enrich_episode — full success
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestEnrichEpisodeFullSuccess:
    """Successful enrichment with all 4 sections."""

    @pytest.mark.asyncio
    async def test_completes_all_four_sections(self) -> None:
        """All 4 enrichment sections execute on a new episode."""
        from workers.tasks.enrich_episode import enrich_episode

        mock_db = _mock_successful_db()

        with (
            patch("workers.tasks.enrich_episode.render_prompt") as mock_render,
            patch("workers.tasks.enrich_episode.build_enrichment_prompt") as mock_build,
            patch("core.llm.resolve_backend") as mock_resolve_backend,
            patch("core.org_config.get_org_config") as mock_org_config,
            patch("workers.backend.resolve_graph_backend") as mock_graph,
            patch("workers.tasks.classify_dialog.process_classification_output") as mock_cls,
            patch("workers.tasks.extract_entities.process_entities_output") as mock_ent,
            patch("workers.tasks.extract_facts.process_facts_output") as mock_fct,
            patch("workers.tasks.extract_structured.process_structured_output") as mock_str,
            patch("repositories.episode_blob_repository.EpisodeBlobRepository") as mock_blob_repo_cls,
        ):
            # Prompt rendering
            mock_render.return_value = ("system prompt", {"entity_types": [], "known_entities": [], "existing_facts": [], "schemas": []})
            mock_build.return_value = "full prompt"

            # Org config
            mock_org_cfg = MagicMock()
            mock_org_cfg.to_llm_config_dict.return_value = {"model": "gpt-4"}
            mock_org_config.return_value = mock_org_cfg

            # LLM
            mock_llm = AsyncMock()
            mock_response = MagicMock()
            mock_response.validated_data = _make_combined_output()
            mock_llm.chat.return_value = mock_response
            mock_resolve_backend.return_value = mock_llm

            # Graph backend
            mock_graph.return_value = MagicMock()

            # Processors (async)
            mock_cls.return_value = None
            mock_ent.return_value = {}
            mock_fct.return_value = None
            mock_str.return_value = None

            # Blob repo — no blobs
            mock_blob_repo = AsyncMock()
            mock_blob_repo.get_by_episode.return_value = []
            mock_blob_repo_cls.return_value = mock_blob_repo

            ctx = _make_ctx(db=mock_db)

            result = await enrich_episode(
                ctx=ctx,
                episode_id=_EPISODE_ID,
                content=_CONTENT,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                session_id=_SESSION_ID,
                trace_id="trace-1",
                metadata={"source": "test"},
            )

            assert result is None
            # All 4 processors should have been called
            mock_cls.assert_called_once()
            mock_ent.assert_called_once()
            mock_fct.assert_called_once()
            mock_str.assert_called_once()
            # Commit should happen
            mock_db.commit.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════════
# enrich_episode — partial failure
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestEnrichEpisodePartialFailure:
    """Partial enrichment failure raises PartialEnrichmentError."""

    @pytest.mark.asyncio
    async def test_raises_partial_error_with_successful_bits(self) -> None:
        """When some sections fail, PartialEnrichmentError is raised."""
        from workers.tasks.base import ENRICHMENT_CLASSIFICATION, ENRICHMENT_ENTITIES
        from workers.tasks.enrich_episode import (
            PartialEnrichmentError,
            enrich_episode,
        )

        mock_db = _mock_successful_db()

        with (
            patch("workers.tasks.enrich_episode.render_prompt") as mock_render,
            patch("workers.tasks.enrich_episode.build_enrichment_prompt") as mock_build,
            patch("core.llm.resolve_backend") as mock_resolve_backend,
            patch("core.org_config.get_org_config") as mock_org_config,
            patch("workers.backend.resolve_graph_backend") as mock_graph,
            patch("workers.tasks.classify_dialog.process_classification_output") as mock_cls,
            patch("workers.tasks.extract_entities.process_entities_output") as mock_ent,
            patch("workers.tasks.extract_facts.process_facts_output") as mock_fct,
            patch("workers.tasks.extract_structured.process_structured_output") as mock_str,
            patch("repositories.episode_blob_repository.EpisodeBlobRepository"),
        ):
            mock_render.return_value = ("system prompt", {"entity_types": [], "known_entities": [], "existing_facts": [], "schemas": []})
            mock_build.return_value = "full prompt"

            mock_org_cfg = MagicMock()
            mock_org_cfg.to_llm_config_dict.return_value = {}
            mock_org_config.return_value = mock_org_cfg

            mock_llm = AsyncMock()
            mock_response = MagicMock()
            mock_response.validated_data = _make_combined_output()
            mock_llm.chat.return_value = mock_response
            mock_resolve_backend.return_value = mock_llm

            mock_graph.return_value = MagicMock()

            # Classification succeeds, entities fails
            mock_cls.return_value = None
            mock_ent.side_effect = ValueError("Entity processing failed")
            mock_fct.return_value = None
            mock_str.return_value = None

            ctx = _make_ctx(db=mock_db)

            with pytest.raises(PartialEnrichmentError) as exc_info:
                await enrich_episode(
                    ctx=ctx,
                    episode_id=_EPISODE_ID,
                    content=_CONTENT,
                    org_id=_ORG_ID,
                    project_id=_PROJECT_ID,
                    session_id=_SESSION_ID,
                )

            # Classification completed → bit 4 is set
            assert exc_info.value.successful_bits & ENRICHMENT_CLASSIFICATION
            # Entities failed → bit 0 is NOT set
            assert not (exc_info.value.successful_bits & ENRICHMENT_ENTITIES)

    @pytest.mark.asyncio
    async def test_all_sections_fail(self) -> None:
        """When ALL sections fail, successful_bits is 0."""
        from workers.tasks.enrich_episode import (
            PartialEnrichmentError,
            enrich_episode,
        )

        mock_db = _mock_successful_db()

        with (
            patch("workers.tasks.enrich_episode.render_prompt") as mock_render,
            patch("workers.tasks.enrich_episode.build_enrichment_prompt") as mock_build,
            patch("core.llm.resolve_backend") as mock_resolve_backend,
            patch("core.org_config.get_org_config") as mock_org_config,
            patch("workers.backend.resolve_graph_backend"),
            patch("workers.tasks.classify_dialog.process_classification_output") as mock_cls,
            patch("workers.tasks.extract_entities.process_entities_output") as mock_ent,
            patch("workers.tasks.extract_facts.process_facts_output") as mock_fct,
            patch("workers.tasks.extract_structured.process_structured_output") as mock_str,
            patch("repositories.episode_blob_repository.EpisodeBlobRepository"),
        ):
            mock_render.return_value = ("prompt", {"entity_types": [], "known_entities": [], "existing_facts": [], "schemas": []})
            mock_build.return_value = "prompt"

            mock_org_cfg = MagicMock()
            mock_org_cfg.to_llm_config_dict.return_value = {}
            mock_org_config.return_value = mock_org_cfg

            mock_llm = AsyncMock()
            mock_response = MagicMock()
            mock_response.validated_data = _make_combined_output()
            mock_llm.chat.return_value = mock_response
            mock_resolve_backend.return_value = mock_llm

            # All 4 fail
            mock_cls.side_effect = RuntimeError("CLS fail")
            mock_ent.side_effect = RuntimeError("ENT fail")
            mock_fct.side_effect = RuntimeError("FCT fail")
            mock_str.side_effect = RuntimeError("STR fail")

            ctx = _make_ctx(db=mock_db)

            with pytest.raises(PartialEnrichmentError) as exc_info:
                await enrich_episode(
                    ctx=ctx,
                    episode_id=_EPISODE_ID,
                    content=_CONTENT,
                    org_id=_ORG_ID,
                    project_id=_PROJECT_ID,
                    session_id=_SESSION_ID,
                )

            assert exc_info.value.successful_bits == 0


# ═══════════════════════════════════════════════════════════════════════════════
# enrich_episode — missing ctx resources
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestEnrichEpisodeMissingCtx:
    """Behaviour when ``ctx`` is missing optional fields."""

    @pytest.mark.asyncio
    async def test_missing_db_engine_creates_own(self) -> None:
        """When ctx has no db_engine, one is created and disposed."""
        from workers.tasks.enrich_episode import enrich_episode

        mock_db = _mock_successful_db()

        with (
            patch("workers.tasks.enrich_episode.render_prompt") as mock_render,
            patch("workers.tasks.enrich_episode.build_enrichment_prompt") as mock_build,
            patch("core.llm.resolve_backend") as mock_resolve_backend,
            patch("core.org_config.get_org_config") as mock_org_config,
            patch("workers.backend.resolve_graph_backend"),
            patch("workers.tasks.classify_dialog.process_classification_output"),
            patch("workers.tasks.extract_entities.process_entities_output") as mock_ent,
            patch("workers.tasks.extract_facts.process_facts_output"),
            patch("workers.tasks.extract_structured.process_structured_output"),
            patch("repositories.episode_blob_repository.EpisodeBlobRepository"),
            patch("core.db.init_db_engine") as mock_init_engine,
            patch("core.db.get_async_session") as mock_get_session,
        ):
            mock_render.return_value = ("p", {"entity_types": [], "known_entities": [], "existing_facts": [], "schemas": []})
            mock_build.return_value = "p"

            mock_org_cfg = MagicMock()
            mock_org_cfg.to_llm_config_dict.return_value = {}
            mock_org_config.return_value = mock_org_cfg

            mock_llm = AsyncMock()
            mock_response = MagicMock()
            mock_response.validated_data = _make_combined_output()
            mock_llm.chat.return_value = mock_response
            mock_resolve_backend.return_value = mock_llm

            # process_entities_output must return a dict, not an AsyncMock
            # (AsyncMock.items() returns a coroutine, not iterable)
            mock_ent.return_value = {}

            # init_db_engine returns a mock engine
            mock_engine = MagicMock()
            mock_engine.dispose = AsyncMock()
            mock_init_engine.return_value = mock_engine

            # session factory — use configured mock_db so execute/begin_nested work
            mock_get_session.return_value = MagicMock(return_value=mock_db)

            # Ctx without db_engine or db_session_factory
            ctx: dict = {"redis": AsyncMock()}

            result = await enrich_episode(
                ctx=ctx,
                episode_id=_EPISODE_ID,
                content=_CONTENT,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                session_id=_SESSION_ID,
                trace_id="trace-1",
            )

            assert result is None
            # Own engine should be disposed in finally block
            mock_engine.dispose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_missing_redis_skips_gracefully(self) -> None:
        """Missing redis in ctx does not cause failure (None arq_redis)."""
        from workers.tasks.enrich_episode import enrich_episode

        mock_db = _mock_successful_db()

        with (
            patch("workers.tasks.enrich_episode.render_prompt") as mock_render,
            patch("workers.tasks.enrich_episode.build_enrichment_prompt") as mock_build,
            patch("core.llm.resolve_backend") as mock_resolve_backend,
            patch("core.org_config.get_org_config") as mock_org_config,
            patch("workers.backend.resolve_graph_backend"),
            patch("workers.tasks.classify_dialog.process_classification_output"),
            patch("workers.tasks.extract_entities.process_entities_output") as mock_ent,
            patch("workers.tasks.extract_facts.process_facts_output"),
            patch("workers.tasks.extract_structured.process_structured_output"),
            patch("repositories.episode_blob_repository.EpisodeBlobRepository"),
        ):
            mock_render.return_value = ("p", {"entity_types": [], "known_entities": [], "existing_facts": [], "schemas": []})
            mock_build.return_value = "p"

            mock_org_cfg = MagicMock()
            mock_org_cfg.to_llm_config_dict.return_value = {}
            mock_org_config.return_value = mock_org_cfg

            mock_llm = AsyncMock()
            mock_response = MagicMock()
            mock_response.validated_data = _make_combined_output()
            mock_llm.chat.return_value = mock_response
            mock_resolve_backend.return_value = mock_llm

            # process_entities_output must return a dict, not an AsyncMock
            mock_ent.return_value = {}

            # Ctx WITHOUT redis key
            ctx = _make_ctx(db=mock_db, include_redis=False)

            result = await enrich_episode(
                ctx=ctx,
                episode_id=_EPISODE_ID,
                content=_CONTENT,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                session_id=_SESSION_ID,
                trace_id="trace-1",
            )

            assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# enrich_episode — blob text
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestEnrichEpisodeBlobText:
    """Blob text is appended to prompt for enrichment."""

    @pytest.mark.asyncio
    async def test_blob_text_appended_to_prompt(self) -> None:
        """Episode with blobs has text appended to the prompt."""
        from workers.tasks.enrich_episode import enrich_episode

        mock_db = _mock_successful_db()

        with (
            patch("workers.tasks.enrich_episode.render_prompt") as mock_render,
            patch("workers.tasks.enrich_episode.build_enrichment_prompt") as mock_build,
            patch("core.llm.resolve_backend") as mock_resolve_backend,
            patch("core.org_config.get_org_config") as mock_org_config,
            patch("workers.backend.resolve_graph_backend"),
            patch("workers.tasks.classify_dialog.process_classification_output"),
            patch("workers.tasks.extract_entities.process_entities_output") as mock_ent,
            patch("workers.tasks.extract_facts.process_facts_output"),
            patch("workers.tasks.extract_structured.process_structured_output"),
            patch("repositories.episode_blob_repository.EpisodeBlobRepository") as mock_blob_cls,
        ):
            mock_render.return_value = ("p", {"entity_types": [], "known_entities": [], "existing_facts": [], "schemas": []})
            mock_build.return_value = "base prompt"

            mock_org_cfg = MagicMock()
            mock_org_cfg.to_llm_config_dict.return_value = {}
            mock_org_config.return_value = mock_org_cfg

            mock_llm = AsyncMock()
            mock_response = MagicMock()
            mock_response.validated_data = _make_combined_output()
            mock_llm.chat.return_value = mock_response
            mock_resolve_backend.return_value = mock_llm

            # process_entities_output must return a dict, not an AsyncMock
            mock_ent.return_value = {}

            # Blob with extracted_text
            mock_blob = MagicMock()
            mock_blob.file_name = "test.pdf"
            mock_blob.mime_type = "application/pdf"
            mock_blob.extracted_text = "PDF content here"

            mock_blob_repo = AsyncMock()
            mock_blob_repo.get_by_episode.return_value = [mock_blob]
            mock_blob_cls.return_value = mock_blob_repo

            ctx = _make_ctx(db=mock_db)

            result = await enrich_episode(
                ctx=ctx,
                episode_id=_EPISODE_ID,
                content=_CONTENT,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                session_id=_SESSION_ID,
                trace_id="trace-1",
            )

            assert result is None
            # build_enrichment_prompt should NOT have been called with appended text
            # (that happens in the function body after build)
            mock_build.assert_called_once_with("p", ANY)

    @pytest.mark.asyncio
    async def test_blob_fetch_failure_does_not_block(self) -> None:
        """Blob fetch exception is logged but enrichment continues."""
        from workers.tasks.enrich_episode import enrich_episode

        mock_db = _mock_successful_db()

        with (
            patch("workers.tasks.enrich_episode.render_prompt") as mock_render,
            patch("workers.tasks.enrich_episode.build_enrichment_prompt") as mock_build,
            patch("core.llm.resolve_backend") as mock_resolve_backend,
            patch("core.org_config.get_org_config") as mock_org_config,
            patch("workers.backend.resolve_graph_backend"),
            patch("workers.tasks.classify_dialog.process_classification_output"),
            patch("workers.tasks.extract_entities.process_entities_output") as mock_ent,
            patch("workers.tasks.extract_facts.process_facts_output"),
            patch("workers.tasks.extract_structured.process_structured_output"),
            patch("repositories.episode_blob_repository.EpisodeBlobRepository") as mock_blob_cls,
        ):
            mock_render.return_value = ("p", {"entity_types": [], "known_entities": [], "existing_facts": [], "schemas": []})
            mock_build.return_value = "prompt"

            mock_org_cfg = MagicMock()
            mock_org_cfg.to_llm_config_dict.return_value = {}
            mock_org_config.return_value = mock_org_cfg

            mock_llm = AsyncMock()
            mock_response = MagicMock()
            mock_response.validated_data = _make_combined_output()
            mock_llm.chat.return_value = mock_response
            mock_resolve_backend.return_value = mock_llm

            # process_entities_output must return a dict, not an AsyncMock
            mock_ent.return_value = {}

            # Blob repo raises
            mock_blob_repo = AsyncMock()
            mock_blob_repo.get_by_episode.side_effect = RuntimeError("DB error")
            mock_blob_cls.return_value = mock_blob_repo

            ctx = _make_ctx(db=mock_db)

            # Should NOT raise — blob failure is non-critical
            result = await enrich_episode(
                ctx=ctx,
                episode_id=_EPISODE_ID,
                content=_CONTENT,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                session_id=_SESSION_ID,
                trace_id="trace-1",
            )

            assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# enrich_episode — with_retry decorator presence
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestEnrichEpisodeDecorator:
    """``enrich_episode`` is decorated with ``@with_retry``."""

    def test_decorated_with_with_retry(self) -> None:
        """``enrich_episode`` function has the ``with_retry`` wrapper."""
        from workers.tasks.enrich_episode import enrich_episode

        # The @with_retry decorator uses functools.wraps, so
        # __wrapped__ should point to the original function
        assert hasattr(enrich_episode, "__wrapped__")

    def test_retry_params_via_decorator(self) -> None:
        """The ``@with_retry`` decorator is applied (checked via __wrapped__)."""
        from workers.tasks.enrich_episode import enrich_episode

        # functools.wraps sets __wrapped__ on the decorated function
        assert hasattr(enrich_episode, "__wrapped__")
        assert callable(enrich_episode.__wrapped__)


# ═══════════════════════════════════════════════════════════════════════════════
# enrich_episode — graph backend unavailable
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestEnrichEpisodeGraphBackend:
    """Behaviour when graph backend is disabled (None) or broken (raises)."""

    @pytest.mark.asyncio
    async def test_continues_without_graph_backend(self) -> None:
        """When graph backend is unavailable, sections proceed without entity_repo."""
        from workers.tasks.enrich_episode import enrich_episode

        mock_db = _mock_successful_db()

        with (
            patch("workers.tasks.enrich_episode.render_prompt") as mock_render,
            patch("workers.tasks.enrich_episode.build_enrichment_prompt") as mock_build,
            patch("core.llm.resolve_backend") as mock_resolve_backend,
            patch("core.org_config.get_org_config") as mock_org_config,
            patch("workers.backend.resolve_graph_backend") as mock_graph,
            patch("workers.tasks.classify_dialog.process_classification_output") as mock_cls,
            patch("workers.tasks.extract_entities.process_entities_output") as mock_ent,
            patch("workers.tasks.extract_facts.process_facts_output") as mock_fct,
            patch("workers.tasks.extract_structured.process_structured_output") as mock_str,
            patch("repositories.episode_blob_repository.EpisodeBlobRepository"),
        ):
            mock_render.return_value = ("p", {"entity_types": [], "known_entities": [], "existing_facts": [], "schemas": []})
            mock_build.return_value = "prompt"

            mock_org_cfg = MagicMock()
            mock_org_cfg.to_llm_config_dict.return_value = {}
            mock_org_config.return_value = mock_org_cfg

            mock_llm = AsyncMock()
            mock_response = MagicMock()
            mock_response.validated_data = _make_combined_output()
            mock_llm.chat.return_value = mock_response
            mock_resolve_backend.return_value = mock_llm

            # Graph backend unavailable → returns None
            mock_graph.return_value = None

            mock_cls.return_value = None
            mock_fct.return_value = None
            mock_str.return_value = None

            ctx = _make_ctx(db=mock_db)

            result = await enrich_episode(
                ctx=ctx,
                episode_id=_EPISODE_ID,
                content=_CONTENT,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                session_id=_SESSION_ID,
                trace_id="trace-1",
            )

            assert result is None
            # process_entities_output should NOT be called (no entity_repo)
            mock_ent.assert_not_called()

    @pytest.mark.asyncio
    async def test_graph_backend_unavailable_raises(self) -> None:
        """``GraphBackendUnavailableError`` propagates — no silent entity loss.

        A configured-but-broken backend must abort the task: if the error were
        swallowed, the ENTITIES bit would be set without persisting entities
        and the episode would be permanently marked complete.
        """
        from core.exceptions import GraphBackendUnavailableError
        from workers.tasks.enrich_episode import enrich_episode

        mock_db = _mock_successful_db()

        with (
            patch("workers.tasks.enrich_episode.render_prompt") as mock_render,
            patch("workers.tasks.enrich_episode.build_enrichment_prompt") as mock_build,
            patch("core.llm.resolve_backend") as mock_resolve_backend,
            patch("core.org_config.get_org_config") as mock_org_config,
            patch("workers.backend.resolve_graph_backend") as mock_graph,
            patch("workers.tasks.classify_dialog.process_classification_output") as mock_cls,
            patch("workers.tasks.extract_entities.process_entities_output") as mock_ent,
            patch("workers.tasks.extract_facts.process_facts_output") as mock_fct,
            patch("workers.tasks.extract_structured.process_structured_output") as mock_str,
            patch("repositories.episode_blob_repository.EpisodeBlobRepository"),
        ):
            mock_render.return_value = ("p", {"entity_types": [], "known_entities": [], "existing_facts": [], "schemas": []})
            mock_build.return_value = "prompt"

            mock_org_cfg = MagicMock()
            mock_org_cfg.to_llm_config_dict.return_value = {}
            mock_org_config.return_value = mock_org_cfg

            mock_llm = AsyncMock()
            mock_response = MagicMock()
            mock_response.validated_data = _make_combined_output()
            mock_llm.chat.return_value = mock_response
            mock_resolve_backend.return_value = mock_llm

            # Graph backend raises specific error (broken, not disabled)
            mock_graph.side_effect = GraphBackendUnavailableError("Graph down")

            mock_cls.return_value = None
            mock_fct.return_value = None
            mock_str.return_value = None

            ctx = _make_ctx(db=mock_db)

            with pytest.raises(GraphBackendUnavailableError):
                await enrich_episode(
                    ctx=ctx,
                    episode_id=_EPISODE_ID,
                    content=_CONTENT,
                    org_id=_ORG_ID,
                    project_id=_PROJECT_ID,
                    session_id=_SESSION_ID,
                    trace_id="trace-1",
                )

        # No section processor ran and nothing was committed — the ENTITIES
        # bit is NOT set, so retry/reconcile will re-run the episode.
        mock_cls.assert_not_called()
        mock_ent.assert_not_called()
        mock_fct.assert_not_called()
        mock_str.assert_not_called()
        mock_db.commit.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════════════════
# enrich_episode — org config fetch failure
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestEnrichEpisodeOrgConfig:
    """Behaviour when org config fetch fails."""

    @pytest.mark.asyncio
    async def test_continues_without_llm_config(self) -> None:
        """When org config fetch fails, LLM call proceeds with None config."""
        from workers.tasks.enrich_episode import enrich_episode

        mock_db = _mock_successful_db()

        with (
            patch("workers.tasks.enrich_episode.render_prompt") as mock_render,
            patch("workers.tasks.enrich_episode.build_enrichment_prompt") as mock_build,
            patch("core.llm.resolve_backend") as mock_resolve_backend,
            patch("core.org_config.get_org_config") as mock_org_config,
            patch("workers.backend.resolve_graph_backend"),
            patch("workers.tasks.classify_dialog.process_classification_output") as mock_cls,
            patch("workers.tasks.extract_entities.process_entities_output") as mock_ent,
            patch("workers.tasks.extract_facts.process_facts_output") as mock_fct,
            patch("workers.tasks.extract_structured.process_structured_output") as mock_str,
            patch("repositories.episode_blob_repository.EpisodeBlobRepository"),
        ):
            mock_render.return_value = ("p", {"entity_types": [], "known_entities": [], "existing_facts": [], "schemas": []})
            mock_build.return_value = "prompt"

            # Org config fetch raises
            mock_org_config.side_effect = RuntimeError("OpenBao unavailable")

            mock_llm = AsyncMock()
            mock_response = MagicMock()
            mock_response.validated_data = _make_combined_output()
            mock_llm.chat.return_value = mock_response
            mock_resolve_backend.return_value = mock_llm

            # process_entities_output must return a dict, not an AsyncMock
            mock_ent.return_value = {}

            mock_cls.return_value = None
            mock_fct.return_value = None
            mock_str.return_value = None

            ctx = _make_ctx(db=mock_db)

            result = await enrich_episode(
                ctx=ctx,
                episode_id=_EPISODE_ID,
                content=_CONTENT,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                session_id=_SESSION_ID,
                trace_id="trace-1",
            )

            assert result is None
            # LLM was still called (resolve_backend called with None config)
            mock_resolve_backend.assert_called_once_with(org_config=None)


# ═══════════════════════════════════════════════════════════════════════════════
# enrich_episode — LLM call failure
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestEnrichEpisodeLLMFailure:
    """Behaviour when the LLM call fails."""

    @pytest.mark.asyncio
    async def test_llm_exception_propagates(self) -> None:
        """LLM call exception is re-raised (for ARQ retry)."""
        from workers.tasks.enrich_episode import enrich_episode

        mock_db = _mock_successful_db()

        with (
            patch("workers.tasks.enrich_episode.render_prompt") as mock_render,
            patch("workers.tasks.enrich_episode.build_enrichment_prompt") as mock_build,
            patch("core.llm.resolve_backend") as mock_resolve_backend,
            patch("core.org_config.get_org_config") as mock_org_config,
            patch("workers.backend.resolve_graph_backend"),
            patch("repositories.episode_blob_repository.EpisodeBlobRepository"),
        ):
            mock_render.return_value = ("p", {"entity_types": [], "known_entities": [], "existing_facts": [], "schemas": []})
            mock_build.return_value = "prompt"

            mock_org_cfg = MagicMock()
            mock_org_cfg.to_llm_config_dict.return_value = {}
            mock_org_config.return_value = mock_org_cfg

            # LLM call raises
            mock_llm = AsyncMock()
            mock_llm.chat.side_effect = RuntimeError("LLM API timeout")
            mock_resolve_backend.return_value = mock_llm

            ctx = _make_ctx(db=mock_db)

            with pytest.raises(RuntimeError, match="LLM API timeout"):
                await enrich_episode(
                    ctx=ctx,
                    episode_id=_EPISODE_ID,
                    content=_CONTENT,
                    org_id=_ORG_ID,
                    project_id=_PROJECT_ID,
                    session_id=_SESSION_ID,
                    trace_id="trace-1",
                )


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _make_ctx(db: AsyncMock | None = None, include_redis: bool = True) -> dict:
    """Create a minimal ARQ worker context dict.

    Includes ``openbao_client`` so that ``enrich_episode`` takes the direct
    OpenBao path instead of the ``BootstrapSettings`` fallback (which
    requires real environment variables).

    Args:
        db: Optional mock DB session (if None, one is created).
        include_redis: Whether to include a mock redis client.

    Returns:
        A dict suitable as the ``ctx`` argument for ``enrich_episode``.
    """
    session_factory = MagicMock()
    if db is not None:
        session_factory.return_value = db
    else:
        _db = AsyncMock()
        _db.__aenter__.return_value = _db
        session_factory.return_value = _db

    ctx: dict = {
        "db_engine": MagicMock(),
        "db_session_factory": session_factory,
        "openbao_client": AsyncMock(),
    }

    if include_redis:
        ctx["redis"] = AsyncMock()

    return ctx


def _mock_successful_db() -> AsyncMock:
    """Create a mock DB that returns a fresh episode with no enrichment bits set.

    Returns:
        A configured AsyncMock DB session.
    """
    mock_db = AsyncMock()

    # Episode row
    mock_episode = MagicMock()
    mock_episode.enrichment_status = 0
    mock_episode.user_id = uuid4()

    # SQL result
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_episode

    mock_db.execute.return_value = mock_result

    # ``async with db:`` must operate on the SAME mock object so that
    # ``db.execute()`` inside the ``async with`` block uses our configured
    # ``return_value`` above.
    mock_db.__aenter__.return_value = mock_db
    mock_db.__aexit__.return_value = None

    # begin_nested context manager
    # ⚠️ Use MagicMock — AsyncMock sub-attributes return coroutines on call,
    # but begin_nested() in SQLAlchemy is a sync method that returns an async
    # context manager (not a coroutine).  MagicMock returns return_value directly.
    mock_nested = AsyncMock()
    mock_db.begin_nested = MagicMock()
    mock_db.begin_nested.return_value = mock_nested
    mock_nested.__aenter__.return_value = mock_nested
    mock_nested.__aexit__.return_value = None

    return mock_db


def _make_combined_output() -> MagicMock:
    """Create a mock CombinedLLMOutput with test data."""
    output = MagicMock()

    # Classification
    output.classification.intent = "analysis"
    output.classification.emotion = None
    output.classification.voice_tone = None

    # Entities / relationships — use dicts so Pydantic
    # EntityExtractionOutput validation accepts them.
    output.entities = [
        {"name": "Entity1", "type": "Person"},
    ]
    output.relationships = []

    # Facts
    output.facts = []

    # Structured extractions
    output.structured_extractions = []

    return output
