"""Unit tests for generate_user_summary task."""

from __future__ import annotations

import itertools
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from uuid import uuid4

import pytest

_ORG_ID = str(uuid4())
_USER_ID = str(uuid4())
_PROJECT_ID = str(uuid4())

# ``generate_user_summary`` resolves the graph backend before rendering and
# raises ``GraphBackendUnavailableError`` when it comes back None, so every
# test needs a non-None backend in the worker context.
_GRAPH_BACKEND_TARGET = "workers.backend.resolve_graph_backend"
_GRAPH_BACKEND = MagicMock()


def _session_recorder(db: AsyncMock) -> Mock:
    """Attach to the session and return a recorder of its ordered calls.

    The order matters: the RLS tenant context is bound with an ``execute``,
    the work is committed, and the context is released with a rollback.
    The release issues no statement of its own — the context is bound
    transaction-locally, so the end of the transaction already discarded
    it.

    Attach it BEFORE the session is used — it only records the calls made
    after it was attached.
    """
    parent = Mock()
    parent.attach_mock(db.execute, "execute")
    parent.attach_mock(db.rollback, "rollback")
    parent.attach_mock(db.commit, "commit")
    return parent


def _session_ops(recorder: Mock) -> list[str]:
    """The ordered operation names recorded by :func:`_session_recorder`."""
    return [call[0] for call in recorder.mock_calls]


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

    def _factory(self, *dbs: AsyncMock) -> MagicMock:
        """Factory handing out one session per ``async with`` block.

        A single session is reused for every block (the historical
        behaviour); pass one session per block to assert on them
        independently — the task opens two: one to resolve the graph
        backend, one to persist the summary.
        """
        f = MagicMock()
        f.side_effect = itertools.cycle(dbs)
        return f

    def _ctx(self, *dbs: AsyncMock) -> dict:
        return {"db_engine": MagicMock(), "db_session_factory": self._factory(*dbs)}

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        """LLM generates summary from user data and persists it."""
        mock_llm = AsyncMock()
        mock_llm.chat.return_value = MagicMock(
            content="User is interested in Python, AI, and distributed systems."
        )

        with (
            patch(
                "workers.tasks.generate_user_summary.with_retry",
                lambda **kw: lambda f: f,
            ),
            patch(_GRAPH_BACKEND_TARGET, return_value=_GRAPH_BACKEND),
            patch(
                "workers.tasks.generate_user_summary.render_prompt",
                return_value="Summarize this user.",
            ),
            patch("core.llm.resolve_backend", return_value=mock_llm),
            patch("core.org_config.get_org_config") as mock_cfg,
            patch("repositories.user_repository.UserRepository") as mock_repo_cls,
            patch("asyncio.sleep", AsyncMock()),
        ):
            mock_cfg.return_value = MagicMock(to_llm_config_dict=lambda: {})

            mock_repo = AsyncMock()
            mock_repo_cls.return_value = mock_repo

            # Two sessions: the task opens one to resolve the graph backend
            # and one to persist the summary.
            backend_db = self._make_db()
            persist_db = self._make_db()
            backend_recorder = _session_recorder(backend_db)
            persist_recorder = _session_recorder(persist_db)
            from workers.tasks.generate_user_summary import generate_user_summary

            await generate_user_summary(
                ctx=self._ctx(backend_db, persist_db),
                org_id=_ORG_ID,
                user_id=_USER_ID,
                project_id=_PROJECT_ID,
            )

            mock_llm.chat.assert_called_once()
            mock_repo.update_summary.assert_called_once()
            # The summary write is committed…
            assert _session_ops(persist_recorder) == [
                "execute",  # the tenant binding
                "commit",  # the summary write
                "rollback",  # release the tenant context
            ]
            assert persist_db.execute.await_args_list[0].args[1] == {
                "org_id": _ORG_ID,
                "bypass_rls": "false",
                "lookup_code": "",
            }
            # The graph-backend session is bound and released the same way.
            assert _session_ops(backend_recorder) == [
                "execute",
                "rollback",
            ]

    @pytest.mark.asyncio
    async def test_empty_history(self) -> None:
        """Empty user history → fallback summary generated."""
        mock_llm = AsyncMock()
        mock_llm.chat.return_value = MagicMock(content="No significant history yet.")

        with (
            patch(
                "workers.tasks.generate_user_summary.with_retry",
                lambda **kw: lambda f: f,
            ),
            patch(_GRAPH_BACKEND_TARGET, return_value=_GRAPH_BACKEND),
            patch(
                "workers.tasks.generate_user_summary.render_prompt",
                return_value="No history.",
            ),
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
            patch(
                "workers.tasks.generate_user_summary.with_retry",
                lambda **kw: lambda f: f,
            ),
            patch(_GRAPH_BACKEND_TARGET, return_value=_GRAPH_BACKEND),
            patch(
                "workers.tasks.generate_user_summary.render_prompt",
                return_value="Prompt.",
            ),
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
            patch(
                "workers.tasks.generate_user_summary.with_retry",
                lambda **kw: lambda f: f,
            ),
            patch(_GRAPH_BACKEND_TARGET, return_value=_GRAPH_BACKEND),
            patch(
                "workers.tasks.generate_user_summary.render_prompt",
                side_effect=Exception("Template error"),
            ),
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
            patch(
                "workers.tasks.generate_user_summary.with_retry",
                lambda **kw: lambda f: f,
            ),
            patch(_GRAPH_BACKEND_TARGET, return_value=_GRAPH_BACKEND),
            patch(
                "workers.tasks.generate_user_summary.render_prompt",
                return_value="Prompt.",
            ),
            patch("core.llm.resolve_backend", return_value=mock_llm),
            patch("core.org_config.get_org_config"),
            patch("repositories.user_repository.UserRepository") as mock_repo_cls,
        ):
            mock_repo = AsyncMock()
            # Prod logs and bare-``raise``s (generate_user_summary.py:228-235)
            # — it does not wrap, so the injected type is the asserted type.
            mock_repo.update_summary.side_effect = RuntimeError("DB write failed")
            mock_repo_cls.return_value = mock_repo

            db = self._make_db()
            from workers.tasks.generate_user_summary import generate_user_summary

            with pytest.raises(RuntimeError, match="DB write failed"):
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
            patch(
                "workers.tasks.generate_user_summary.with_retry",
                lambda **kw: lambda f: f,
            ),
            patch(_GRAPH_BACKEND_TARGET, return_value=_GRAPH_BACKEND),
            patch(
                "workers.tasks.generate_user_summary.render_prompt",
                return_value="Prompt.",
            ),
            patch("core.llm.resolve_backend", return_value=mock_llm),
            patch(
                "core.org_config.get_org_config",
                side_effect=Exception("Config fetch failed"),
            ),
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
        with patch(
            "workers.tasks.generate_user_summary.with_retry", lambda **kw: lambda f: f
        ):
            db = AsyncMock()
            # ``session_factory()`` fails at __aenter__; the surrounding
            # ``except GraphBackendUnavailableError`` does not catch it, so it
            # propagates unwrapped (generate_user_summary.py:108-121).
            db.__aenter__.side_effect = ConnectionError("Connection refused")

            from workers.tasks.generate_user_summary import generate_user_summary

            with pytest.raises(ConnectionError, match="Connection refused"):
                await generate_user_summary(
                    ctx=self._ctx(db),
                    org_id=_ORG_ID,
                    user_id=_USER_ID,
                )
