"""Unit tests for embed_fact task."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

_FACT_ID = str(uuid4())
_ORG_ID = str(uuid4())
_CONTENT = "Test fact content for embedding."
_TRACE_ID = "trace-202"


@pytest.mark.unit
class TestEmbedFact:
    """embed_fact task tests."""

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

    def _make_org_config(self, **overrides) -> MagicMock:
        cfg = MagicMock()
        cfg.embedding_backend = overrides.get("embedding_backend", "openai")
        cfg.embedding_model = overrides.get("embedding_model", "text-embedding-3-small")
        cfg.embedding_dim = overrides.get("embedding_dim", 1536)
        return cfg

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        """Fact embedding generated and stored successfully."""
        embedding = [0.2] * 1536

        with (
            patch("core.org_config.get_org_config") as mock_cfg,
            patch("core.llm.resolve_backend") as mock_llm_cls,
        ):
            mock_cfg.return_value = self._make_org_config()

            mock_llm = AsyncMock()
            mock_result = MagicMock()
            mock_result.embeddings = [embedding]
            mock_llm.embed.return_value = mock_result
            mock_llm_cls.return_value = mock_llm

            db = self._make_db()
            from workers.tasks.embed_fact import embed_fact

            await embed_fact(
                ctx=self._ctx(db),
                fact_id=_FACT_ID,
                org_id=_ORG_ID,
                content=_CONTENT,
                trace_id=_TRACE_ID,
            )

            mock_llm.embed.assert_called_once()

    @pytest.mark.asyncio
    async def test_fact_not_found(self) -> None:
        """Missing fact logs and returns (does not raise)."""
        with (
            patch("core.org_config.get_org_config") as mock_cfg,
        ):
            mock_cfg.return_value = self._make_org_config()

            db = self._make_db()
            db.execute.return_value.one_or_none.return_value = None

            from workers.tasks.embed_fact import embed_fact

            # content=None triggers DB fetch which returns nothing → log + return
            await embed_fact(
                ctx=self._ctx(db),
                fact_id=_FACT_ID,
                org_id=_ORG_ID,
                content=None,
            )

    @pytest.mark.asyncio
    async def test_no_embedding_backend(self) -> None:
        """No embedding backend configured → raises."""
        with (
            patch("core.org_config.get_org_config") as mock_cfg,
        ):
            cfg = self._make_org_config()
            cfg.embedding_backend = None
            mock_cfg.return_value = cfg

            db = self._make_db()
            ctx = self._ctx(db)

            from workers.tasks.embed_fact import embed_fact

            with pytest.raises(Exception):
                await embed_fact(
                    ctx=ctx,
                    fact_id=_FACT_ID,
                    org_id=_ORG_ID,
                    content=_CONTENT,
                )

    @pytest.mark.asyncio
    async def test_embedding_failure(self) -> None:
        """Embedding API failure propagates."""
        with (
            patch("core.org_config.get_org_config") as mock_cfg,
            patch("core.llm.resolve_backend") as mock_llm_cls,
        ):
            mock_cfg.return_value = self._make_org_config()

            mock_llm = AsyncMock()
            mock_llm.embed.side_effect = Exception("Embedding API error")
            mock_llm_cls.return_value = mock_llm

            db = self._make_db()
            ctx = self._ctx(db)

            from workers.tasks.embed_fact import embed_fact

            with pytest.raises(Exception, match="Embedding API error"):
                await embed_fact(
                    ctx=ctx,
                    fact_id=_FACT_ID,
                    org_id=_ORG_ID,
                    content=_CONTENT,
                )

    @pytest.mark.asyncio
    async def test_empty_content(self) -> None:
        """Empty content still generates embedding."""
        embedding = [0.2] * 1536

        with (
            patch("core.org_config.get_org_config") as mock_cfg,
            patch("core.llm.resolve_backend") as mock_llm_cls,
        ):
            mock_cfg.return_value = self._make_org_config()

            mock_llm = AsyncMock()
            mock_result = MagicMock()
            mock_result.embeddings = [embedding]
            mock_llm.embed.return_value = mock_result
            mock_llm_cls.return_value = mock_llm

            db = self._make_db()
            ctx = self._ctx(db)

            from workers.tasks.embed_fact import embed_fact

            await embed_fact(
                ctx=ctx,
                fact_id=_FACT_ID,
                org_id=_ORG_ID,
                content="",
            )

            mock_llm.embed.assert_called_once()

    @pytest.mark.asyncio
    async def test_lazy_import(self) -> None:
        """Import renaming reflects error pattern."""
        from workers.tasks.embed_fact import embed_fact

        assert callable(embed_fact)

    @pytest.mark.asyncio
    async def test_dimension_mismatch(self) -> None:
        """Embedding dimension mismatch raises ValueError."""
        embedding = [0.2] * 512  # Wrong dimension

        with (
            patch("core.org_config.get_org_config") as mock_cfg,
            patch("core.llm.resolve_backend") as mock_llm_cls,
        ):
            mock_cfg.return_value = self._make_org_config(embedding_dim=1536)

            mock_llm = AsyncMock()
            mock_result = MagicMock()
            mock_result.embeddings = [embedding]
            mock_llm.embed.return_value = mock_result
            mock_llm_cls.return_value = mock_llm

            db = self._make_db()
            ctx = self._ctx(db)

            from workers.tasks.embed_fact import embed_fact

            with pytest.raises(ValueError, match="dimension mismatch"):
                await embed_fact(
                    ctx=ctx,
                    fact_id=_FACT_ID,
                    org_id=_ORG_ID,
                    content=_CONTENT,
                )

    # ── Coverage gap: engine/session/bao_client edge cases ──────────────────

    @pytest.mark.asyncio
    async def test_no_db_engine_in_ctx(self) -> None:
        """Missing db_engine → creates own engine + session factory, disposes."""
        embedding = [0.2] * 1536

        with (
            patch("core.org_config.get_org_config") as mock_cfg,
            patch("core.llm.resolve_backend") as mock_llm_cls,
            patch("core.db.init_db_engine") as mock_init_engine,
            patch("core.db.get_async_session") as mock_get_session,
        ):
            mock_cfg.return_value = self._make_org_config()

            mock_llm = AsyncMock()
            mock_result = MagicMock()
            mock_result.embeddings = [embedding]
            mock_llm.embed.return_value = mock_result
            mock_llm_cls.return_value = mock_llm

            mock_engine = AsyncMock()
            mock_engine.dispose = AsyncMock()
            mock_init_engine.return_value = mock_engine
            mock_session_factory = MagicMock()
            mock_get_session.return_value = mock_session_factory

            db = self._make_db()
            mock_session_factory.return_value = db

            # ctx WITHOUT db_engine or db_session_factory → triggers lazy init
            ctx = {"openbao_client": MagicMock()}

            from workers.tasks.embed_fact import embed_fact

            await embed_fact(
                ctx=ctx,
                fact_id=_FACT_ID,
                org_id=_ORG_ID,
                content=_CONTENT,
            )

            mock_llm.embed.assert_called_once()
            mock_init_engine.assert_called_once()
            mock_get_session.assert_called_once_with(mock_engine)
            mock_engine.dispose.assert_called_once()

    @pytest.mark.asyncio
    async def test_content_fetched_from_db(self) -> None:
        """Content not provided → fetched from DB successfully."""
        embedding = [0.2] * 1536
        db_content = "fact content from database"

        with (
            patch("core.org_config.get_org_config") as mock_cfg,
            patch("core.llm.resolve_backend") as mock_llm_cls,
        ):
            mock_cfg.return_value = self._make_org_config()

            mock_llm = AsyncMock()
            mock_result = MagicMock()
            mock_result.embeddings = [embedding]
            mock_llm.embed.return_value = mock_result
            mock_llm_cls.return_value = mock_llm

            db = self._make_db()
            row = MagicMock()
            row.__getitem__.return_value = db_content
            db.execute.return_value.one_or_none.return_value = row

            from workers.tasks.embed_fact import embed_fact

            await embed_fact(
                ctx=self._ctx(db),
                fact_id=_FACT_ID,
                org_id=_ORG_ID,
                content=None,
            )

            mock_llm.embed.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_bao_client_in_ctx(self) -> None:
        """No openbao_client in ctx → uses BootstrapSettings + OpenBaoClient."""
        embedding = [0.2] * 1536

        with (
            patch("core.org_config.get_org_config") as mock_cfg,
            patch("core.llm.resolve_backend") as mock_llm_cls,
            patch("core.config.BootstrapSettings") as mock_bs,
            patch("core.openbao.OpenBaoClient") as mock_bao_cls,
        ):
            mock_cfg.return_value = self._make_org_config()

            mock_llm = AsyncMock()
            mock_result = MagicMock()
            mock_result.embeddings = [embedding]
            mock_llm.embed.return_value = mock_result
            mock_llm_cls.return_value = mock_llm

            mock_bao = MagicMock()
            mock_bao.__aenter__ = AsyncMock(return_value=mock_bao)
            mock_bao.__aexit__ = AsyncMock(return_value=None)
            mock_bao_cls.return_value = mock_bao

            db = self._make_db()
            # ctx WITHOUT openbao_client → triggers BootstrapSettings path
            ctx = {"db_engine": MagicMock(), "db_session_factory": self._factory(db)}

            from workers.tasks.embed_fact import embed_fact

            await embed_fact(
                ctx=ctx,
                fact_id=_FACT_ID,
                org_id=_ORG_ID,
                content=_CONTENT,
            )

            mock_llm.embed.assert_called_once()
            mock_bs.assert_called_once()
            mock_bao_cls.assert_called_once()

    @pytest.mark.asyncio
    async def test_org_config_none(self) -> None:
        """get_org_config returns None → raises RuntimeError."""
        with (
            patch("core.org_config.get_org_config") as mock_cfg,
        ):
            mock_cfg.return_value = None

            db = self._make_db()
            from workers.tasks.embed_fact import embed_fact

            with pytest.raises(RuntimeError, match="Org config not found"):
                await embed_fact(
                    ctx=self._ctx(db),
                    fact_id=_FACT_ID,
                    org_id=_ORG_ID,
                    content=_CONTENT,
                )

    @pytest.mark.asyncio
    async def test_org_config_fetch_raises(self) -> None:
        """get_org_config raises → caught, logged, re-raised as RuntimeError.

        Covers the except Exception block (lines 116-122) in the org config
        fetch try/except.
        """
        with (
            patch("core.org_config.get_org_config") as mock_cfg,
        ):
            mock_cfg.side_effect = ValueError("Bao returned garbage")

            db = self._make_db()
            from workers.tasks.embed_fact import embed_fact

            with pytest.raises(RuntimeError, match="Failed to fetch org config"):
                await embed_fact(
                    ctx=self._ctx(db),
                    fact_id=_FACT_ID,
                    org_id=_ORG_ID,
                    content=_CONTENT,
                )
