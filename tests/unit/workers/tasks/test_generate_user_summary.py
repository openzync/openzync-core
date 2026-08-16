"""Unit tests for generate_user_summary task."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

_ORG_ID = str(uuid4())
_USER_ID = str(uuid4())
_PROJECT_ID = str(uuid4())


@pytest.mark.unit
class TestGenerateUserSummary:
    """generate_user_summary task tests."""

    def _make_db(self) -> AsyncMock:
        db = AsyncMock()
        # ``add`` is sync in SQLAlchemy — an AsyncMock child would return an
        # unawaited coroutine (RuntimeWarning → error under filterwarnings).
        db.add = MagicMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = None
        return db

    def _factory(self, db: AsyncMock) -> MagicMock:
        f = MagicMock()
        f.return_value = db
        return f

    def _ctx(self, db: AsyncMock) -> dict:
        return {"db_engine": MagicMock(), "db_session_factory": self._factory(db)}

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        """LLM generates summary from user data and persists it."""
        mock_llm = AsyncMock()
        mock_llm.chat.return_value = MagicMock(content="User is interested in Python, AI, and distributed systems.")

        with (
            patch("workers.tasks.generate_user_summary.with_retry", lambda **kw: lambda f: f),
            patch("workers.tasks.generate_user_summary.render_prompt", return_value="Summarize this user."),
            patch("core.llm.resolve_backend", return_value=mock_llm),
            patch("core.org_config.get_org_config") as mock_cfg,
            patch("repositories.user_repository.UserRepository") as mock_repo_cls,
            patch("asyncio.sleep", AsyncMock()),
        ):
            mock_cfg.return_value = MagicMock(to_llm_config_dict=lambda: {})

            mock_repo = AsyncMock()
            mock_repo_cls.return_value = mock_repo

            db = self._make_db()
            from workers.tasks.generate_user_summary import generate_user_summary

            await generate_user_summary(
                ctx=self._ctx(db),
                org_id=_ORG_ID,
                user_id=_USER_ID,
                project_id=_PROJECT_ID,
            )

            mock_llm.chat.assert_called_once()
            mock_repo.update_summary.assert_called_once()
            db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_history(self) -> None:
        """Empty user history → fallback summary generated."""
        mock_llm = AsyncMock()
        mock_llm.chat.return_value = MagicMock(content="No significant history yet.")

        with (
            patch("workers.tasks.generate_user_summary.with_retry", lambda **kw: lambda f: f),
            patch("workers.tasks.generate_user_summary.render_prompt", return_value="No history."),
            patch("core.llm.resolve_backend", return_value=mock_llm),
            patch("core.org_config.get_org_config"),
            patch("repositories.user_repository.UserRepository") as mock_repo_cls,
        ):
            mock_repo = AsyncMock()
            mock_repo_cls.return_value = mock_repo

            db = self._make_db()
            from workers.tasks.generate_user_summary import generate_user_summary

            await generate_user_summary(
                ctx=self._ctx(db),
                org_id=_ORG_ID,
                user_id=_USER_ID,
            )

            mock_repo.update_summary.assert_called_once()

    @pytest.mark.asyncio
    async def test_llm_failure(self) -> None:
        """LLM failure → graceful degradation (exception propagates for retry)."""
        with (
            patch("workers.tasks.generate_user_summary.with_retry", lambda **kw: lambda f: f),
            patch("workers.tasks.generate_user_summary.render_prompt", return_value="Prompt."),
            patch("core.llm.resolve_backend", side_effect=Exception("LLM timeout")),
            patch("core.org_config.get_org_config"),
        ):
            db = self._make_db()
            from workers.tasks.generate_user_summary import generate_user_summary

            with pytest.raises(Exception, match="LLM timeout"):
                await generate_user_summary(
                    ctx=self._ctx(db),
                    org_id=_ORG_ID,
                    user_id=_USER_ID,
                )

    @pytest.mark.asyncio
    async def test_prompt_render_failure(self) -> None:
        """Prompt rendering failure propagates."""
        with (
            patch("workers.tasks.generate_user_summary.with_retry", lambda **kw: lambda f: f),
            patch("workers.tasks.generate_user_summary.render_prompt", side_effect=Exception("Template error")),
        ):
            from workers.tasks.generate_user_summary import generate_user_summary

            with pytest.raises(Exception, match="Template error"):
                await generate_user_summary(
                    ctx=self._ctx(self._make_db()),
                    org_id=_ORG_ID,
                    user_id=_USER_ID,
                )

    @pytest.mark.asyncio
    async def test_persist_failure(self) -> None:
        """DB persist failure propagates."""
        mock_llm = AsyncMock()
        mock_llm.chat.return_value = MagicMock(content="A summary.")

        with (
            patch("workers.tasks.generate_user_summary.with_retry", lambda **kw: lambda f: f),
            patch("workers.tasks.generate_user_summary.render_prompt", return_value="Prompt."),
            patch("core.llm.resolve_backend", return_value=mock_llm),
            patch("core.org_config.get_org_config"),
            patch("repositories.user_repository.UserRepository") as mock_repo_cls,
        ):
            mock_repo = AsyncMock()
            mock_repo.update_summary.side_effect = Exception("DB write failed")
            mock_repo_cls.return_value = mock_repo

            db = self._make_db()
            from workers.tasks.generate_user_summary import generate_user_summary

            with pytest.raises(Exception):
                await generate_user_summary(
                    ctx=self._ctx(db),
                    org_id=_ORG_ID,
                    user_id=_USER_ID,
                )

    @pytest.mark.asyncio
    async def test_org_config_fetch_failure(self) -> None:
        """Org config fetch failure → graceful degradation (falls through)."""
        mock_llm = AsyncMock()
        mock_llm.chat.return_value = MagicMock(content="Fallback summary.")

        with (
            patch("workers.tasks.generate_user_summary.with_retry", lambda **kw: lambda f: f),
            patch("workers.tasks.generate_user_summary.render_prompt", return_value="Prompt."),
            patch("core.llm.resolve_backend", return_value=mock_llm),
            patch("core.org_config.get_org_config", side_effect=Exception("Config fetch failed")),
            patch("repositories.user_repository.UserRepository") as mock_repo_cls,
        ):
            mock_repo = AsyncMock()
            mock_repo_cls.return_value = mock_repo

            db = self._make_db()
            from workers.tasks.generate_user_summary import generate_user_summary

            await generate_user_summary(
                ctx=self._ctx(db),
                org_id=_ORG_ID,
                user_id=_USER_ID,
            )

            # Falls through with None llm_config_dict
            mock_llm.chat.assert_called_once()
            mock_repo.update_summary.assert_called_once()

    @pytest.mark.asyncio
    async def test_db_error_propagates(self) -> None:
        """Database connection error propagates."""
        with patch("workers.tasks.generate_user_summary.with_retry", lambda **kw: lambda f: f):
            db = AsyncMock()
            db.__aenter__.side_effect = Exception("Connection refused")

            from workers.tasks.generate_user_summary import generate_user_summary

            with pytest.raises(Exception):
                await generate_user_summary(
                    ctx=self._ctx(db),
                    org_id=_ORG_ID,
                    user_id=_USER_ID,
                )
