"""Unit tests for extract_structured task."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

_EPISODE_ID = str(uuid4())
_ORG_ID = str(uuid4())
_PROJECT_ID = str(uuid4())
_SESSION_ID = str(uuid4())
_CONTENT = "John bought a MacBook Pro for $2,499."
_TRACE_ID = "trace-789"


@pytest.mark.unit
class TestExtractStructured:
    """extract_structured task tests."""

    def _make_db(self) -> AsyncMock:
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = None
        db.execute.return_value = MagicMock()
        return db

    def _factory(self, db: AsyncMock) -> MagicMock:
        f = MagicMock()
        f.return_value = db
        return f

    def _ctx(self, db: AsyncMock) -> dict:
        return {
            "db_engine": MagicMock(),
            "db_session_factory": self._factory(db),
            "openbao_client": MagicMock(),
        }

    def _make_llm_response(self, data: dict | None = None) -> MagicMock:
        parsed = MagicMock()
        parsed.model_dump.return_value = data or {
            "purchase": {
                "product": "MacBook Pro",
                "price": 2499.0,
                "currency": "USD",
            }
        }
        resp = MagicMock()
        resp.validated_data = parsed
        return resp

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        """Structured data extracted and persisted successfully."""
        llm_resp = self._make_llm_response()

        with (
            patch("workers.tasks.extract_structured.render_prompt",
                  return_value=("system", {
                      "schemas": [{"id": str(uuid4()), "name": "purchase", "json_schema": {}}],
                  })),
            patch("workers.tasks.extract_structured.build_enrichment_prompt",
                  return_value="prompt"),
            patch("core.llm.resolve_backend") as mock_llm_cls,
            patch("workers.backend.resolve_graph_backend", return_value=AsyncMock()),
            patch("core.org_config.get_org_config") as mock_org_cfg,
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
            patch("core.config.settings") as _,
            patch("services.worker.worker_settings.get_worker_settings") as mock_ws,
        ):
            mock_ws.return_value = MagicMock(
                STRUCTURED_EXTRACTION_MAX_TOKENS=512,
            )
            mock_llm = AsyncMock()
            mock_llm.chat.return_value = llm_resp
            mock_llm_cls.return_value = mock_llm

            mock_org_cfg.return_value = MagicMock()

            episode = MagicMock()
            episode.id = _EPISODE_ID
            episode.enrichment_status = 0

            mock_ep_repo = AsyncMock()
            mock_ep_repo.get_by_id_for_update.return_value = episode
            mock_ep_repo_cls.return_value = mock_ep_repo

            db = self._make_db()
            from workers.tasks.extract_structured import extract_structured

            await extract_structured(
                ctx=self._ctx(db),
                episode_id=_EPISODE_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                session_id=_SESSION_ID,
                content=_CONTENT,
                trace_id=_TRACE_ID,
            )

            mock_llm.chat.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_schemas(self) -> None:
        """No schemas configured → set enrichment bit and return."""
        with (
            patch("workers.tasks.extract_structured.render_prompt",
                  return_value=("system prompt", {"schemas": []})),
            patch("workers.tasks.extract_structured.build_enrichment_prompt",
                  return_value="prompt"),
            patch("core.org_config.get_org_config") as mock_org_cfg,
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
        ):
            mock_org_cfg.return_value = MagicMock()

            episode = MagicMock()
            episode.id = _EPISODE_ID
            episode.enrichment_status = 0

            mock_ep_repo = AsyncMock()
            mock_ep_repo.get_by_id_for_update.return_value = episode
            mock_ep_repo_cls.return_value = mock_ep_repo

            db = self._make_db()
            from workers.tasks.extract_structured import extract_structured

            await extract_structured(
                ctx=self._ctx(db),
                episode_id=_EPISODE_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                session_id=_SESSION_ID,
                content=_CONTENT,
            )

            mock_ep_repo.apply_enrichment_bits.assert_called_once()

    @pytest.mark.asyncio
    async def test_already_extracted(self) -> None:
        """Bit 5 already set → no-op."""
        with (
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
        ):
            episode = MagicMock()
            episode.id = _EPISODE_ID
            episode.enrichment_status = 1 << 5

            mock_ep_repo = AsyncMock()
            mock_ep_repo.get_by_id_for_update.return_value = episode
            mock_ep_repo_cls.return_value = mock_ep_repo

            db = self._make_db()
            from workers.tasks.extract_structured import extract_structured

            await extract_structured(
                ctx=self._ctx(db),
                episode_id=_EPISODE_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                session_id=_SESSION_ID,
                content=_CONTENT,
            )

            mock_ep_repo.apply_enrichment_bits.assert_not_called()

    @pytest.mark.asyncio
    async def test_episode_not_found(self) -> None:
        """Missing episode raises exception."""
        with (
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
        ):
            mock_ep_repo = AsyncMock()
            mock_ep_repo.get_by_id_for_update.return_value = None
            mock_ep_repo_cls.return_value = mock_ep_repo

            db = self._make_db()
            from workers.tasks.extract_structured import extract_structured

            with pytest.raises(Exception):
                await extract_structured(
                    ctx=self._ctx(db),
                    episode_id=_EPISODE_ID,
                    org_id=_ORG_ID,
                    project_id=_PROJECT_ID,
                    session_id=_SESSION_ID,
                    content=_CONTENT,
                )

    @pytest.mark.asyncio
    async def test_llm_error(self) -> None:
        """LLM call failure propagates."""
        with (
            patch("workers.tasks.extract_structured.render_prompt",
                  return_value=("system prompt", {
                      "schemas": [{"id": str(uuid4()), "name": "test", "json_schema": {}}],
                  })),
            patch("workers.tasks.extract_structured.build_enrichment_prompt",
                  return_value="prompt"),
            patch("core.llm.resolve_backend") as mock_llm_cls,
            patch("workers.backend.resolve_graph_backend", return_value=AsyncMock()),
            patch("core.org_config.get_org_config") as mock_org_cfg,
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
            patch("core.config.settings") as _,
            patch("services.worker.worker_settings.get_worker_settings") as mock_ws,
        ):
            mock_ws.return_value = MagicMock(
                STRUCTURED_EXTRACTION_MAX_TOKENS=512,
            )
            mock_llm = AsyncMock()
            mock_llm.chat.side_effect = Exception("LLM timeout")
            mock_llm_cls.return_value = mock_llm

            mock_org_cfg.return_value = MagicMock()

            episode = MagicMock()
            episode.id = _EPISODE_ID
            episode.enrichment_status = 0

            mock_ep_repo = AsyncMock()
            mock_ep_repo.get_by_id_for_update.return_value = episode
            mock_ep_repo_cls.return_value = mock_ep_repo

            db = self._make_db()
            from workers.tasks.extract_structured import extract_structured

            with pytest.raises(Exception, match="LLM timeout"):
                await extract_structured(
                    ctx=self._ctx(db),
                    episode_id=_EPISODE_ID,
                    org_id=_ORG_ID,
                    project_id=_PROJECT_ID,
                    session_id=_SESSION_ID,
                    content=_CONTENT,
                )

    # ── Coverage gap: engine/session/backend/bao_client edge cases ──────────

    @pytest.mark.asyncio
    async def test_no_db_engine_in_ctx(self) -> None:
        """Missing db_engine → creates own engine and disposes."""
        llm_resp = self._make_llm_response()

        with (
            patch("workers.tasks.extract_structured.render_prompt",
                  return_value=("system", {
                      "schemas": [{"id": str(uuid4()), "name": "purchase", "json_schema": {}}],
                  })),
            patch("workers.tasks.extract_structured.build_enrichment_prompt",
                  return_value="prompt"),
            patch("core.llm.resolve_backend") as mock_llm_cls,
            patch("workers.backend.resolve_graph_backend", return_value=AsyncMock()),
            patch("core.org_config.get_org_config") as mock_org_cfg,
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
            patch("core.config.settings") as _,
            patch("services.worker.worker_settings.get_worker_settings") as mock_ws,
            patch("core.db.init_db_engine") as mock_init_engine,
            patch("core.db.get_async_session") as mock_get_session,
        ):
            mock_ws.return_value = MagicMock(STRUCTURED_EXTRACTION_MAX_TOKENS=512)
            mock_llm = AsyncMock()
            mock_llm.chat.return_value = llm_resp
            mock_llm_cls.return_value = mock_llm
            mock_org_cfg.return_value = MagicMock()

            mock_engine = AsyncMock()
            mock_engine.dispose = AsyncMock()
            mock_init_engine.return_value = mock_engine
            mock_session_factory = MagicMock()
            mock_get_session.return_value = mock_session_factory

            episode = MagicMock()
            episode.id = _EPISODE_ID
            episode.enrichment_status = 0

            mock_ep_repo = AsyncMock()
            mock_ep_repo.get_by_id_for_update.return_value = episode
            mock_ep_repo_cls.return_value = mock_ep_repo

            db = self._make_db()
            mock_session_factory.return_value = db

            # ctx WITHOUT db_engine or db_session_factory
            ctx = {"openbao_client": MagicMock()}

            from workers.tasks.extract_structured import extract_structured

            await extract_structured(
                ctx=ctx,
                episode_id=_EPISODE_ID, org_id=_ORG_ID,
                project_id=_PROJECT_ID, session_id=_SESSION_ID,
                content=_CONTENT,
            )

            mock_llm.chat.assert_called_once()
            mock_init_engine.assert_called_once()
            mock_get_session.assert_called_once()
            mock_engine.dispose.assert_called_once()

    @pytest.mark.asyncio
    async def test_graph_backend_resolve_fails(self) -> None:
        """Graph backend resolve failure → logged, continues."""
        llm_resp = self._make_llm_response()

        with (
            patch("workers.tasks.extract_structured.render_prompt",
                  return_value=("system", {
                      "schemas": [{"id": str(uuid4()), "name": "purchase", "json_schema": {}}],
                  })),
            patch("workers.tasks.extract_structured.build_enrichment_prompt",
                  return_value="prompt"),
            patch("core.llm.resolve_backend") as mock_llm_cls,
            patch("workers.backend.resolve_graph_backend",
                  side_effect=Exception("Backend unavailable")),
            patch("core.org_config.get_org_config") as mock_org_cfg,
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
            patch("core.config.settings") as _,
            patch("services.worker.worker_settings.get_worker_settings") as mock_ws,
        ):
            mock_ws.return_value = MagicMock(STRUCTURED_EXTRACTION_MAX_TOKENS=512)
            mock_llm = AsyncMock()
            mock_llm.chat.return_value = llm_resp
            mock_llm_cls.return_value = mock_llm
            mock_org_cfg.return_value = MagicMock()

            episode = MagicMock()
            episode.id = _EPISODE_ID
            episode.enrichment_status = 0

            mock_ep_repo = AsyncMock()
            mock_ep_repo.get_by_id_for_update.return_value = episode
            mock_ep_repo_cls.return_value = mock_ep_repo

            db = self._make_db()
            from workers.tasks.extract_structured import extract_structured

            await extract_structured(
                ctx=self._ctx(db),
                episode_id=_EPISODE_ID, org_id=_ORG_ID,
                project_id=_PROJECT_ID, session_id=_SESSION_ID,
                content=_CONTENT,
            )

            mock_llm.chat.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_bao_client_in_ctx(self) -> None:
        """No openbao_client in ctx → uses BootstrapSettings + OpenBaoClient."""
        llm_resp = self._make_llm_response()

        with (
            patch("workers.tasks.extract_structured.render_prompt",
                  return_value=("system", {
                      "schemas": [{"id": str(uuid4()), "name": "purchase", "json_schema": {}}],
                  })),
            patch("workers.tasks.extract_structured.build_enrichment_prompt",
                  return_value="prompt"),
            patch("core.llm.resolve_backend") as mock_llm_cls,
            patch("workers.backend.resolve_graph_backend", return_value=AsyncMock()),
            patch("core.org_config.get_org_config") as mock_org_cfg,
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
            patch("core.config.settings") as _,
            patch("services.worker.worker_settings.get_worker_settings") as mock_ws,
            patch("core.config.BootstrapSettings") as mock_bs,
            patch("core.openbao.OpenBaoClient") as mock_bao_cls,
        ):
            mock_ws.return_value = MagicMock(STRUCTURED_EXTRACTION_MAX_TOKENS=512)
            mock_llm = AsyncMock()
            mock_llm.chat.return_value = llm_resp
            mock_llm_cls.return_value = mock_llm
            mock_org_cfg.return_value = MagicMock()

            mock_bao = MagicMock()
            mock_bao.__aenter__ = AsyncMock(return_value=mock_bao)
            mock_bao.__aexit__ = AsyncMock(return_value=None)
            mock_bao_cls.return_value = mock_bao

            episode = MagicMock()
            episode.id = _EPISODE_ID
            episode.enrichment_status = 0

            mock_ep_repo = AsyncMock()
            mock_ep_repo.get_by_id_for_update.return_value = episode
            mock_ep_repo_cls.return_value = mock_ep_repo

            db = self._make_db()
            # ctx WITHOUT openbao_client
            ctx = {"db_engine": MagicMock(), "db_session_factory": self._factory(db)}

            from workers.tasks.extract_structured import extract_structured

            await extract_structured(
                ctx=ctx,
                episode_id=_EPISODE_ID, org_id=_ORG_ID,
                project_id=_PROJECT_ID, session_id=_SESSION_ID,
                content=_CONTENT,
            )

            mock_llm.chat.assert_called_once()
            mock_bs.assert_called_once()
            mock_bao_cls.assert_called_once()

    # ── process_structured_output edge cases ───────────────────────────────

    @pytest.mark.asyncio
    async def test_process_empty_parsed(self) -> None:
        """Empty parsed dict → returns early."""
        db = AsyncMock()
        from workers.tasks.extract_structured import process_structured_output

        await process_structured_output(
            db=db, org_id=_ORG_ID, episode_id=_EPISODE_ID,
            project_id=_PROJECT_ID, session_id=_SESSION_ID,
            parsed={}, schemas=[{"name": "test", "id": str(uuid4()), "json_schema": {}}],
        )
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_unknown_schema(self) -> None:
        """Unknown schema name → warning and skip."""
        db = AsyncMock()
        from workers.tasks.extract_structured import process_structured_output

        await process_structured_output(
            db=db, org_id=_ORG_ID, episode_id=_EPISODE_ID,
            project_id=_PROJECT_ID, session_id=_SESSION_ID,
            parsed={"unknown_name": {"field": "value"}},
            schemas=[{"name": "known_schema", "id": str(uuid4()), "json_schema": {}}],
        )
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_none_data(self) -> None:
        """None data value → skipped."""
        db = AsyncMock()
        from workers.tasks.extract_structured import process_structured_output

        await process_structured_output(
            db=db, org_id=_ORG_ID, episode_id=_EPISODE_ID,
            project_id=_PROJECT_ID, session_id=_SESSION_ID,
            parsed={"test_schema": None},
            schemas=[{"name": "test_schema", "id": str(uuid4()), "json_schema": {}}],
        )
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_non_dict_data(self) -> None:
        """Non-dict data → warning and skip."""
        db = AsyncMock()
        from workers.tasks.extract_structured import process_structured_output

        await process_structured_output(
            db=db, org_id=_ORG_ID, episode_id=_EPISODE_ID,
            project_id=_PROJECT_ID, session_id=_SESSION_ID,
            parsed={"test_schema": "string_data"},
            schemas=[{"name": "test_schema", "id": str(uuid4()), "json_schema": {}}],
        )
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_missing_required_fields(self) -> None:
        """Missing required fields → filled with defaults."""
        db = AsyncMock()
        from workers.tasks.extract_structured import process_structured_output

        with patch(
            "workers.tasks.extract_structured._validate_against_schema",
        ) as mock_validate:
            schema_id = str(uuid4())
            await process_structured_output(
                db=db, org_id=_ORG_ID, episode_id=_EPISODE_ID,
                project_id=_PROJECT_ID, session_id=_SESSION_ID,
                parsed={"test_schema": {"present": "value"}},
                schemas=[{
                    "name": "test_schema", "id": schema_id,
                    "json_schema": {
                        "type": "object",
                        "required": ["required_field"],
                        "properties": {
                            "required_field": {"type": "string"},
                            "present": {"type": "string"},
                        },
                    },
                }],
            )
            mock_validate.assert_called_once()
            cleaned = mock_validate.call_args[0][0]
            assert cleaned.get("required_field") == "unknown"
            assert cleaned.get("present") == "value"

    @pytest.mark.asyncio
    async def test_process_validation_failure(self) -> None:
        """Schema validation failure → warning and continue."""
        db = AsyncMock()
        from workers.tasks.extract_structured import process_structured_output

        with patch(
            "workers.tasks.extract_structured._validate_against_schema",
            side_effect=Exception("invalid data"),
        ):
            schema_id = str(uuid4())
            await process_structured_output(
                db=db, org_id=_ORG_ID, episode_id=_EPISODE_ID,
                project_id=_PROJECT_ID, session_id=_SESSION_ID,
                parsed={"test_schema": {"field": "value"}},
                schemas=[{"name": "test_schema", "id": schema_id, "json_schema": {}}],
            )
            db.execute.assert_not_called()
