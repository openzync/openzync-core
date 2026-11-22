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
        from workers.tasks.base import ENRICHMENT_ENTITIES, ENRICHMENT_FACTS
        from workers.tasks.enrich_episode import PartialEnrichmentError

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
                user_id=_ORG_ID,
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
            LLM_INVALIDATION_BIT,
        )
        from workers.tasks.enrich_episode import enrich_episode

        # Mirrors enrich_episode's top-level idempotency mask exactly
        # (bits 0, 2, 4, 5 + LLM_INVALIDATION_BIT = bit 8).
        all_llm_bits = (
            ENRICHMENT_ENTITIES
            | ENRICHMENT_FACTS
            | ENRICHMENT_CLASSIFICATION
            | ENRICHMENT_STRUCTURED_EXTRACTION
            | LLM_INVALIDATION_BIT
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
            user_id=_ORG_ID,
            project_id=_PROJECT_ID,
            session_id=_SESSION_ID,
            trace_id="trace-1",
        )

        assert result is None
        # LLM call should NOT have been made
        mock_db.begin_nested.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_invalidation_bit_does_not_return_early(self) -> None:
        """Bits 0,2,4,5 WITHOUT bit 8 is NOT done — the pass runs and stamps it.

        The top-level idempotency mask requires all five LLM bits including
        ``LLM_INVALIDATION_BIT``.  A pre-invalidation episode (entities,
        facts, classification, structured set; bit 8 clear) must run the
        flow — the facts else-branch stamps bit 8 and the transaction
        commits — never the early-return path.
        """
        from workers.tasks.base import (
            ENRICHMENT_CLASSIFICATION,
            ENRICHMENT_ENTITIES,
            ENRICHMENT_FACTS,
            ENRICHMENT_STRUCTURED_EXTRACTION,
        )
        from workers.tasks.enrich_episode import enrich_episode

        old_llm_bits = (
            ENRICHMENT_ENTITIES
            | ENRICHMENT_FACTS
            | ENRICHMENT_CLASSIFICATION
            | ENRICHMENT_STRUCTURED_EXTRACTION
        )
        mock_db = _mock_successful_db(enrichment_status=old_llm_bits)

        with (
            patch("workers.tasks.enrich_episode.render_prompt") as mock_render,
            patch("workers.tasks.enrich_episode.build_enrichment_prompt") as mock_build,
            patch("core.llm.resolve_backend") as mock_resolve_backend,
            patch("core.org_config.get_org_config") as mock_org_config,
            patch("workers.backend.resolve_graph_backend"),
            patch("workers.tasks.classify_dialog.process_classification_output"),
            patch("workers.tasks.extract_entities.process_entities_output"),
            patch("workers.tasks.extract_facts.process_facts_output"),
            patch("workers.tasks.extract_structured.process_structured_output"),
            patch("repositories.episode_blob_repository.EpisodeBlobRepository"),
        ):
            mock_render.return_value = (
                "p",
                {
                    "entity_types": [],
                    "known_entities": [],
                    "existing_facts": [],
                    "schemas": [],
                },
            )
            mock_build.return_value = "prompt"

            mock_org_cfg = MagicMock()
            mock_org_cfg.to_llm_config_dict.return_value = {}
            mock_org_config.return_value = mock_org_cfg

            mock_llm = AsyncMock()
            mock_response = MagicMock()
            mock_response.validated_data = _make_combined_output()
            mock_llm.chat.return_value = mock_response
            mock_resolve_backend.return_value = mock_llm

            ctx = _make_ctx(db=mock_db)

            result = await enrich_episode(
                ctx=ctx,
                episode_id=_EPISODE_ID,
                content=_CONTENT,
                user_id=_ORG_ID,
                project_id=_PROJECT_ID,
                session_id=_SESSION_ID,
                trace_id="trace-1",
            )

            # The flow ran to completion — NOT the early-return path.
            assert result is None
            mock_resolve_backend.assert_awaited_once()
            mock_db.commit.assert_awaited_once()


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
                user_id=_ORG_ID,
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
                    user_id=_ORG_ID,
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
                    user_id=_ORG_ID,
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

            # Ctx without db_engine or db_session_factory — but WITH an
            # openbao_client (same shape as _make_ctx) so enrich_episode
            # takes the direct OpenBao path instead of the real
            # BootstrapSettings/OpenBaoClient fallback (live approle login).
            ctx: dict = {"redis": AsyncMock(), "openbao_client": AsyncMock()}

            result = await enrich_episode(
                ctx=ctx,
                episode_id=_EPISODE_ID,
                content=_CONTENT,
                user_id=_ORG_ID,
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
                user_id=_ORG_ID,
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
        """Episode with blobs: ``build_enrichment_prompt`` gets the
        ``include_*`` call shape and the LLM receives blob text appended.

        Contract: all five LLM ``include_*`` flags are forwarded explicitly
        (all-``True`` here); blob ``extracted_text`` is appended to the
        rendered prompt *after* the build call, so the builder is never
        called with appended text.
        """
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
                user_id=_ORG_ID,
                project_id=_PROJECT_ID,
                session_id=_SESSION_ID,
                trace_id="trace-1",
            )

            assert result is None
            # build_enrichment_prompt should NOT have been called with appended text
            # (that happens in the function body after build) — instead the
            # include_* call shape carries every LLM flag explicitly.
            mock_build.assert_called_once_with(
                "p",
                ANY,
                include_classification=True,
                include_entities=True,
                include_facts=True,
                include_structured=True,
                include_invalidations=True,
            )
            # The LLM receives the built prompt with the blob text appended.
            sent_messages = mock_llm.chat.await_args.args[0]
            sent_prompt = sent_messages[1]["content"]
            assert sent_prompt.startswith("base prompt")
            assert "PDF content here" in sent_prompt

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
                user_id=_ORG_ID,
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
                user_id=_ORG_ID,
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
                    user_id=_ORG_ID,
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
    async def test_fails_loud_without_llm_config(self) -> None:
        """Org config fetch failure aborts loudly — no silent LLM fallback."""
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

            # Org config fetch raises → fail loud (no silent default).
            mock_org_config.side_effect = RuntimeError("OpenBao unavailable")

            # process_entities_output must return a dict, not an AsyncMock
            mock_ent.return_value = {}

            mock_cls.return_value = None
            mock_fct.return_value = None
            mock_str.return_value = None

            ctx = _make_ctx(db=mock_db)

            with pytest.raises(RuntimeError, match="OpenBao unavailable"):
                await enrich_episode(
                    ctx=ctx,
                    episode_id=_EPISODE_ID,
                    content=_CONTENT,
                    user_id=_ORG_ID,
                    project_id=_PROJECT_ID,
                    session_id=_SESSION_ID,
                    trace_id="trace-1",
                )

            # The LLM was never reached and nothing was committed.
            mock_resolve_backend.assert_not_called()
            mock_db.commit.assert_not_awaited()


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
                    user_id=_ORG_ID,
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


def _mock_successful_db(enrichment_status: int = 0) -> AsyncMock:
    """Create a mock DB that returns a fresh episode with no enrichment bits set.

    Args:
        enrichment_status: Bitmask to set on the episode row (defaults to
            0 — a brand-new episode).

    Returns:
        A configured AsyncMock DB session.
    """
    mock_db = AsyncMock()

    # ``add`` is sync in SQLAlchemy — an AsyncMock child would return an
    # unawaited coroutine (RuntimeWarning → error under filterwarnings).
    mock_db.add = MagicMock()

    # Episode row
    mock_episode = MagicMock()
    mock_episode.enrichment_status = enrichment_status
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


# ═══════════════════════════════════════════════════════════════════════════════
# Toggle-gated enrichment (C12 / D13–D17 / F24 / F26)
#
# One parametrized class pins the whole toggle contract end to end:
# - C(12): a partially-enriched episode is NOT done (both bitmask checks).
# - D(13)+F(24): each disabled toggle (x9) mark-skips its bit, skips its
#   persistence, hides its OUTPUT SCHEMA block, and skips its DB providers.
# - D(14): all-disabled commits and returns without an LLM call.
# - D(15): enabled siblings run with facts off.
# - D(16): unknown ARQ key → non-retryable ValidationError.
# - D(17): llm_invalidation is ANDed with the org opt-out.
# - F(26): llm_call_start logs the enabled set.
# ═══════════════════════════════════════════════════════════════════════════════


def _render_ctx() -> dict:
    """Context for the REAL template: every conditional block can render."""
    from uuid import uuid4 as _uuid4

    return {
        "entity_types": ["Person"],
        "known_entities": [{"name": "Known", "entity_type": "Person"}],
        "existing_facts": [
            {
                "id": str(_uuid4()),
                "subject": "S",
                "predicate": "P",
                "object": "O",
            }
        ],
        "schemas": [],
        "conversation": "Hello world",
        "recent_history": [],
        "similar_episodes": [],
        "related_facts": [],
    }


def _stamped_bits(mock_db: AsyncMock) -> set[int]:
    """Bitmask values passed to ``apply_enrichment_bits`` (UPDATE params)."""
    stamped: set[int] = set()
    for call in mock_db.execute.call_args_list:
        args = call.args
        if len(args) >= 2 and isinstance(args[1], dict) and "bitmask" in args[1]:
            stamped.add(args[1]["bitmask"])
    return stamped


@pytest.mark.unit
class TestPartialMaskIsNotDone:
    """C(12): entities-bit-only (and other partial masks) is NOT done.

    Regression: both ``(status & bits) == bits`` checks must stay
    parenthesized — an unparenthesized ``status & bits == bits`` parses as
    ``status & (bits == bits)`` and an entities-only episode would wrongly
    early-return, permanently skipping enrichment.
    """

    @pytest.mark.parametrize(
        "status",
        [
            1 << 0,  # entities-bit-only
            1 << 2,  # facts-only
            1 << 4,  # classification-only
            1 << 5,  # structured-only
            1 << 8,  # invalidation-only
            (1 << 0) | (1 << 2),  # entities + facts
            (1 << 0) | (1 << 2) | (1 << 4) | (1 << 5),  # pre-invalidation mask
        ],
        ids=[
            "entities-only",
            "facts-only",
            "classification-only",
            "structured-only",
            "invalidation-only",
            "entities+facts",
            "old-four-bit-mask",
        ],
    )
    @pytest.mark.asyncio
    async def test_partial_mask_runs_full_flow(self, status: int) -> None:
        """A partial mask runs the LLM pass — never the early-return path."""
        from workers.tasks.enrich_episode import enrich_episode

        mock_db = _mock_successful_db(enrichment_status=status)

        with (
            patch("workers.tasks.enrich_episode.render_prompt") as mock_render,
            patch("core.llm.resolve_backend") as mock_resolve_backend,
            patch("core.org_config.get_org_config") as mock_org_config,
            patch("workers.backend.resolve_graph_backend"),
            patch("workers.tasks.classify_dialog.process_classification_output"),
            patch("workers.tasks.extract_entities.process_entities_output") as mock_ent,
            patch("workers.tasks.extract_facts.process_facts_output"),
            patch("workers.tasks.extract_structured.process_structured_output"),
            patch("repositories.episode_blob_repository.EpisodeBlobRepository"),
        ):
            from services.worker.prompt_renderer import (
                resolve_prompt_template_by_type,
            )

            mock_render.return_value = (
                resolve_prompt_template_by_type("enrich_episode"),
                _render_ctx(),
            )
            mock_ent.return_value = {}

            mock_org_cfg = MagicMock()
            mock_org_cfg.to_llm_config_dict.return_value = {}
            mock_org_config.return_value = mock_org_cfg

            mock_llm = AsyncMock()
            mock_response = MagicMock()
            mock_response.validated_data = _make_combined_output()
            mock_llm.chat.return_value = mock_response
            mock_resolve_backend.return_value = mock_llm

            ctx = _make_ctx(db=mock_db)

            result = await enrich_episode(
                ctx=ctx,
                episode_id=_EPISODE_ID,
                content=_CONTENT,
                user_id=_ORG_ID,
                project_id=_PROJECT_ID,
                session_id=_SESSION_ID,
            )

            assert result is None
            mock_resolve_backend.assert_awaited_once()
            mock_llm.chat.assert_awaited_once()
            mock_db.commit.assert_awaited_once()


@pytest.mark.unit
class TestDisabledToggleContract:
    """D(13) + F(24): per-toggle-off contract, parametrized x9.

    For the 5 LLM keys this worker owns (entities, facts, classification,
    structured, llm_invalidation): the bit is mark-skipped, the section
    never persists, the OUTPUT SCHEMA block is absent from the rendered
    prompt, and the key's DB providers are skipped (``skip_sources``).

    The other 4 keys (embedding, entity_links, observations, blob_text)
    belong to sibling workers — this worker ignores them, so the full LLM
    pass still runs with no skips.  That no-op is the observed contract.
    """

    _LLM_KEYS = {
        "entities",
        "facts",
        "classification",
        "structured",
        "llm_invalidation",
    }

    @pytest.mark.parametrize(
        "key",
        [
            "entities",
            "embedding",
            "facts",
            "entity_links",
            "classification",
            "structured",
            "observations",
            "blob_text",
            "llm_invalidation",
        ],
    )
    @pytest.mark.asyncio
    async def test_disabled_toggle_contract(self, key: str) -> None:
        """Each toggle off → stamp + skip persistence + hide block + skip IO."""
        from workers.tasks.enrich_episode import enrich_episode

        from core.enrichment_bits import ENRICHMENT_KEYS, ENRICHMENT_KEY_BITS
        from services.worker.prompt_renderer import (
            DataSource,
            resolve_prompt_template_by_type,
        )

        assert set(ENRICHMENT_KEYS) == {
            "entities",
            "embedding",
            "facts",
            "entity_links",
            "classification",
            "structured",
            "observations",
            "blob_text",
            "llm_invalidation",
        }

        enabled = {k: True for k in ENRICHMENT_KEYS}
        enabled[key] = False

        expected_skips: dict[str, set] = {
            "entities": {
                DataSource.SESSION_ENTITIES,
                DataSource.ORG_ENTITY_TYPES,
            },
            "facts": {DataSource.SESSION_FACTS, DataSource.SIMILAR_FACTS},
            "structured": {DataSource.ORG_STRUCTURED_SCHEMAS},
            "classification": {DataSource.ORG_CLASSIFICATION_LABELS},
        }
        expected_skip = expected_skips.get(key, set())

        block_markers: dict[str, list[str]] = {
            "entities": ["### 2. entities", "### 3. relationships"],
            "facts": ["### 4. facts"],
            "classification": ["### 1. classification"],
            "structured": ["### 5. structured_extractions"],
            "llm_invalidation": ["## INVALIDATIONS"],
        }

        mock_db = _mock_successful_db()

        with (
            patch("workers.tasks.enrich_episode.render_prompt") as mock_render,
            # NOTE: build_enrichment_prompt is intentionally NOT mocked —
            # the block-absent assertions need the real Jinja render.
            patch("core.llm.resolve_backend") as mock_resolve_backend,
            patch("core.org_config.get_org_config") as mock_org_config,
            patch("workers.backend.resolve_graph_backend"),
            patch(
                "workers.tasks.classify_dialog.process_classification_output"
            ) as mock_cls,
            patch(
                "workers.tasks.extract_entities.process_entities_output"
            ) as mock_ent,
            patch("workers.tasks.extract_facts.process_facts_output") as mock_fct,
            patch(
                "workers.tasks.extract_structured.process_structured_output"
            ) as mock_str,
            patch("repositories.episode_blob_repository.EpisodeBlobRepository"),
            patch(
                "services.fact_invalidation_service.FactInvalidationService"
            ) as mock_inval_svc,
        ):
            mock_render.return_value = (
                resolve_prompt_template_by_type("enrich_episode"),
                _render_ctx(),
            )
            mock_ent.return_value = {}

            mock_org_cfg = MagicMock()
            mock_org_cfg.to_llm_config_dict.return_value = {}
            mock_org_config.return_value = mock_org_cfg

            mock_llm = AsyncMock()
            mock_response = MagicMock()
            parsed = _make_combined_output()
            if key == "llm_invalidation":
                # Non-empty invalidations prove the service is skipped
                # because the toggle is off — never because there was
                # nothing to invalidate.
                parsed.invalidations = [
                    {
                        "existing_fact_ref": "E1",
                        "action": "invalidate",
                        "reason": "Retracted.",
                        "successor_fact_ref": None,
                    }
                ]
            mock_response.validated_data = parsed
            mock_llm.chat.return_value = mock_response
            mock_resolve_backend.return_value = mock_llm

            ctx = _make_ctx(db=mock_db)

            result = await enrich_episode(
                ctx=ctx,
                episode_id=_EPISODE_ID,
                content=_CONTENT,
                user_id=_ORG_ID,
                project_id=_PROJECT_ID,
                session_id=_SESSION_ID,
                enrichments=enabled,
            )

            assert result is None

            # ── Providers skipped exactly per the worker's skip map ──
            assert (
                mock_render.await_args.kwargs["skip_sources"] == expected_skip
            )

            # ── Prompt blocks: toggled section absent, siblings present ──
            sent_prompt = mock_llm.chat.await_args.args[0][1]["content"]
            for section, markers in block_markers.items():
                for marker in markers:
                    if section == key:
                        assert marker not in sent_prompt
                    else:
                        assert marker in sent_prompt

            stamped = _stamped_bits(mock_db)
            if key in self._LLM_KEYS:
                # ── Bit mark-skipped ──
                assert ENRICHMENT_KEY_BITS[key] in stamped
                # ── Section never persists ──
                if key == "entities":
                    mock_ent.assert_not_called()
                elif key == "facts":
                    mock_fct.assert_not_called()
                elif key == "classification":
                    mock_cls.assert_not_called()
                elif key == "structured":
                    mock_str.assert_not_called()
                elif key == "llm_invalidation":
                    mock_inval_svc.assert_not_called()
                    # Facts still run — only invalidation is gated.
                    mock_fct.assert_called_once()
            else:
                # ── Sibling-worker keys are no-ops here: full pass runs ──
                assert ENRICHMENT_KEY_BITS[key] not in stamped
                mock_cls.assert_called_once()
                mock_ent.assert_called_once()
                mock_fct.assert_called_once()
                mock_str.assert_called_once()


@pytest.mark.unit
class TestAllDisabledCommitsWithoutLLM:
    """D(14): every toggle off → stamp, commit, return; ``llm.chat`` sleeps."""

    @pytest.mark.asyncio
    async def test_all_disabled_no_llm_call(self) -> None:
        """All-disabled commits the mark-skipped bits and never calls the LLM."""
        from workers.tasks.enrich_episode import enrich_episode

        from core.enrichment_bits import ENRICHMENT_KEYS

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
            ctx = _make_ctx(db=mock_db)

            result = await enrich_episode(
                ctx=ctx,
                episode_id=_EPISODE_ID,
                content=_CONTENT,
                user_id=_ORG_ID,
                project_id=_PROJECT_ID,
                session_id=_SESSION_ID,
                enrichments={k: False for k in ENRICHMENT_KEYS},
            )

            assert result is None
            # Mark-skipped path commits and returns before any LLM work.
            mock_db.commit.assert_awaited_once()
            mock_resolve_backend.assert_not_called()
            mock_render.assert_not_called()
            mock_build.assert_not_called()
            mock_cls.assert_not_called()
            mock_ent.assert_not_called()
            mock_fct.assert_not_called()
            mock_str.assert_not_called()


@pytest.mark.unit
class TestEnabledSiblingsRunWithFactsOff:
    """D(15): disabling facts skips only the facts section."""

    @pytest.mark.asyncio
    async def test_facts_off_siblings_run(self) -> None:
        """Facts off → facts processor skipped, siblings + LLM still run."""
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
            mock_render.return_value = (
                "p",
                {
                    "entity_types": [],
                    "known_entities": [],
                    "existing_facts": [],
                    "schemas": [],
                },
            )
            mock_build.return_value = "prompt"
            mock_ent.return_value = {}

            mock_org_cfg = MagicMock()
            mock_org_cfg.to_llm_config_dict.return_value = {}
            mock_org_config.return_value = mock_org_cfg

            mock_llm = AsyncMock()
            mock_response = MagicMock()
            mock_response.validated_data = _make_combined_output()
            mock_llm.chat.return_value = mock_response
            mock_resolve_backend.return_value = mock_llm

            ctx = _make_ctx(db=mock_db)

            result = await enrich_episode(
                ctx=ctx,
                episode_id=_EPISODE_ID,
                content=_CONTENT,
                user_id=_ORG_ID,
                project_id=_PROJECT_ID,
                session_id=_SESSION_ID,
                enrichments={"facts": False, "llm_invalidation": False},
            )

            assert result is None
            mock_llm.chat.assert_awaited_once()
            mock_fct.assert_not_called()
            mock_cls.assert_called_once()
            mock_ent.assert_called_once()
            mock_str.assert_called_once()
            mock_db.commit.assert_awaited_once()


@pytest.mark.unit
class TestUnknownArqKeyNonRetryable:
    """D(16): an unknown ARQ enrichment key fails fast — never retried."""

    @pytest.mark.asyncio
    async def test_unknown_key_raises_non_retryable_validation_error(self) -> None:
        """Unknown key → ``ValidationError`` and ``_is_retryable`` is False."""
        from core.exceptions import ValidationError
        from workers.tasks.enrich_episode import _is_retryable, enrich_episode

        mock_db = _mock_successful_db()
        ctx = _make_ctx(db=mock_db)

        with pytest.raises(ValidationError, match="Unknown enrichment keys"):
            await enrich_episode(
                ctx=ctx,
                episode_id=_EPISODE_ID,
                content=_CONTENT,
                user_id=_ORG_ID,
                project_id=_PROJECT_ID,
                session_id=_SESSION_ID,
                enrichments={"bogus_key": True},  # type: ignore[dict-item]
            )

        assert _is_retryable(ValidationError("Unknown enrichment keys")) is False


@pytest.mark.unit
class TestInvalidationAndedWithOrgOptOut:
    """D(17): the org ``llm_fact_invalidation_enabled=false`` opt-out ANDs
    with the per-episode ``llm_invalidation`` toggle — either off suppresses
    the invalidation service while facts still persist."""

    @pytest.mark.asyncio
    async def test_org_opt_out_suppresses_invalidation(self) -> None:
        """Toggle on + org opt-out off → facts persist, invalidations skipped."""
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
            patch("workers.tasks.extract_facts.process_facts_output") as mock_fct,
            patch("workers.tasks.extract_structured.process_structured_output"),
            patch("repositories.episode_blob_repository.EpisodeBlobRepository"),
            patch(
                "services.fact_invalidation_service.FactInvalidationService"
            ) as mock_inval_svc,
        ):
            mock_render.return_value = (
                "p",
                {
                    "entity_types": [],
                    "known_entities": [],
                    "existing_facts": [
                        {"id": str(uuid4()), "subject": "S", "predicate": "P", "object": "O"}
                    ],
                    "schemas": [],
                },
            )
            mock_build.return_value = "prompt"
            mock_ent.return_value = {}

            # Org opts OUT of LLM-driven invalidation (default is opt-in).
            mock_org_cfg = MagicMock()
            mock_org_cfg.to_llm_config_dict.return_value = {}
            mock_org_cfg.llm_fact_invalidation_enabled = False
            mock_org_config.return_value = mock_org_cfg

            mock_llm = AsyncMock()
            mock_response = MagicMock()
            parsed = _make_combined_output()
            parsed.invalidations = [
                {
                    "existing_fact_ref": "E1",
                    "action": "invalidate",
                    "reason": "Superseded.",
                    "successor_fact_ref": None,
                }
            ]
            mock_response.validated_data = parsed
            mock_llm.chat.return_value = mock_response
            mock_resolve_backend.return_value = mock_llm

            ctx = _make_ctx(db=mock_db)

            result = await enrich_episode(
                ctx=ctx,
                episode_id=_EPISODE_ID,
                content=_CONTENT,
                user_id=_ORG_ID,
                project_id=_PROJECT_ID,
                session_id=_SESSION_ID,
                enrichments={"llm_invalidation": True},
            )

            assert result is None
            # Facts still persist …
            mock_fct.assert_called_once()
            # … but the invalidation service never runs.
            mock_inval_svc.assert_not_called()
            mock_db.commit.assert_awaited_once()


@pytest.mark.unit
class TestLlmCallStartLogsEnabledSet:
    """F(26): ``enrich_episode.llm_call_start`` carries the enabled set."""

    @pytest.mark.asyncio
    async def test_llm_call_start_logs_enabled_sections(self) -> None:
        """The start event lists exactly the enabled toggles, sorted."""
        from workers.tasks.enrich_episode import enrich_episode

        from structlog.testing import capture_logs

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
            mock_render.return_value = (
                "p",
                {
                    "entity_types": [],
                    "known_entities": [],
                    "existing_facts": [],
                    "schemas": [],
                },
            )
            mock_build.return_value = "prompt"
            mock_ent.return_value = {}

            mock_org_cfg = MagicMock()
            mock_org_cfg.to_llm_config_dict.return_value = {}
            mock_org_config.return_value = mock_org_cfg

            mock_llm = AsyncMock()
            mock_response = MagicMock()
            mock_response.validated_data = _make_combined_output()
            mock_llm.chat.return_value = mock_response
            mock_resolve_backend.return_value = mock_llm

            ctx = _make_ctx(db=mock_db)

            with capture_logs() as logs:
                result = await enrich_episode(
                    ctx=ctx,
                    episode_id=_EPISODE_ID,
                    content=_CONTENT,
                    user_id=_ORG_ID,
                    project_id=_PROJECT_ID,
                    session_id=_SESSION_ID,
                    enrichments={"facts": False, "classification": False},
                )

            assert result is None
            starts = [
                log
                for log in logs
                if log.get("event") == "enrich_episode.llm_call_start"
            ]
            assert len(starts) == 1
            assert starts[0]["enabled_sections"] == [
                "blob_text",
                "embedding",
                "entities",
                "entity_links",
                "llm_invalidation",
                "observations",
                "structured",
            ]
