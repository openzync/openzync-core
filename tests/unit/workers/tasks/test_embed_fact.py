"""Unit tests for embed_fact task."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core.exceptions import ExternalServiceError

_FACT_ID = str(uuid4())
_ORG_ID = str(uuid4())
_CONTENT = "Test fact content for embedding."
_TRACE_ID = "trace-202"


@pytest.fixture
def embedder():
    """Patch the single embedder entrypoint; auto-undone after the test.

    Yields:
        The ``AsyncMock`` standing in for ``embed_passage``.
    """
    mock = AsyncMock(return_value=[[0.2] * 768])
    with patch("core.embeddings.embed_passage", mock):
        yield mock


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
        }

    @pytest.mark.asyncio
    async def test_success(self, embedder: AsyncMock) -> None:
        """Fact embedding generated and stored successfully."""

        db = self._make_db()
        from workers.tasks.embed_fact import embed_fact

        await embed_fact(
            ctx=self._ctx(db),
            fact_id=_FACT_ID,
            org_id=_ORG_ID,
            content=_CONTENT,
            trace_id=_TRACE_ID,
        )

    @pytest.mark.asyncio
    async def test_fact_not_found(self) -> None:
        """Missing fact logs and returns (does not raise)."""

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
    async def test_no_org_config_consulted(self, embedder: AsyncMock) -> None:
        """Fact embedding needs no org config, even when one is unavailable.

        Replaces the old "no embedding backend configured" case: there is no
        such configuration any more, so the guarantee is that ``org_id`` is
        accepted by the task but never used to resolve a provider.
        """
        with patch("core.org_config.get_org_config") as mock_get_cfg:
            db = self._make_db()

            from workers.tasks.embed_fact import embed_fact

            await embed_fact(
                ctx=self._ctx(db),
                fact_id=_FACT_ID,
                org_id=_ORG_ID,
                content=_CONTENT,
            )

            mock_get_cfg.assert_not_called()
            embedder.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_embedding_failure(self, embedder: AsyncMock) -> None:
        """Embedder failure propagates."""
        embedder.side_effect = Exception("ONNX inference error")

        db = self._make_db()
        ctx = self._ctx(db)

        from workers.tasks.embed_fact import embed_fact

        with pytest.raises(Exception, match="ONNX inference error"):
            await embed_fact(
                ctx=ctx,
                fact_id=_FACT_ID,
                org_id=_ORG_ID,
                content=_CONTENT,
            )

    @pytest.mark.asyncio
    async def test_empty_content(self, embedder: AsyncMock) -> None:
        """Empty content still generates embedding."""

        db = self._make_db()
        ctx = self._ctx(db)

        from workers.tasks.embed_fact import embed_fact

        await embed_fact(
            ctx=ctx,
            fact_id=_FACT_ID,
            org_id=_ORG_ID,
            content="",
        )

    @pytest.mark.asyncio
    async def test_lazy_import(self) -> None:
        """Import renaming reflects error pattern."""
        from workers.tasks.embed_fact import embed_fact

        assert callable(embed_fact)

    @pytest.mark.asyncio
    async def test_dimension_mismatch(self) -> None:
        """A non-canonical-dim vector raises ExternalServiceError (fail loud).

        The canonical model is ``nomic-embed-text:v1.5`` at 768 dims —
        anything else is refused, never stored.

        Note the fake is installed at the *HTTP client* level, not at
        ``embed_passage``: dimension validation now lives inside the
        embedder, so stubbing the entrypoint would bypass the very check
        this test exists to prove.  The real ``embed_passage`` runs and
        rejects the 512-wide vector itself.  Retry backoff sleeps are
        stubbed (the mismatch is retryable-transient by status, but every
        attempt fails identically).
        """
        import numpy as np

        class _WrongDimResponse:
            """Minimal ``httpx.Response`` carrying wrong-width vectors."""

            def __init__(self, count: int) -> None:
                self._count = count

            def raise_for_status(self) -> None:
                pass

            def json(self) -> dict:
                # Non-zero filler on purpose: ``embed_passage`` normalises
                # before validating width, so an all-zeros row would trip
                # the zero-norm guard first and mask the width check.
                return {
                    "embeddings": [
                        np.ones(512, dtype=np.float32).tolist()
                        for _ in range(self._count)
                    ]
                }

        class _WrongDimClient:
            """Stands in for the module-level ``httpx.AsyncClient``."""

            async def post(self, url: str, json: dict | None = None, **_kwargs: object) -> _WrongDimResponse:
                return _WrongDimResponse(len((json or {}).get("input", [])))

        db = self._make_db()
        ctx = self._ctx(db)

        from core import embeddings as embeddings_mod
        from workers.tasks.embed_fact import embed_fact

        with (
            patch.object(embeddings_mod, "_CLIENT", _WrongDimClient()),
            patch("workers.tasks.base.asyncio.sleep", new=AsyncMock()),
            pytest.raises(ExternalServiceError) as exc_info,
        ):
            await embed_fact(
                ctx=ctx,
                fact_id=_FACT_ID,
                org_id=_ORG_ID,
                content=_CONTENT,
            )

        # The message is assembled dynamically (it interpolates the actual
        # length and CANONICAL_EMBED_DIM), so assert on the stable literal
        # tail plus the structured detail rather than a brittle regex.
        assert "Refusing to store." in str(exc_info.value)
        assert exc_info.value.detail == {
            "source": "embed_passage",
            "got": 512,
            "expected": 768,
        }

    # ── Coverage gap: engine/session/bao_client edge cases ──────────────────

    @pytest.mark.asyncio
    async def test_no_db_engine_in_ctx(self, embedder: AsyncMock) -> None:
        """Missing db_engine → creates own engine + session factory, disposes."""

        with (
            patch("core.db.init_db_engine") as mock_init_engine,
            patch("core.db.get_async_session") as mock_get_session,
        ):
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

            embedder.assert_awaited_once()
            mock_init_engine.assert_called_once()
            mock_get_session.assert_called_once_with(mock_engine)
            mock_engine.dispose.assert_called_once()

    @pytest.mark.asyncio
    async def test_content_fetched_from_db(self, embedder: AsyncMock) -> None:
        """Content not provided → fetched from DB successfully."""
        db_content = "fact content from database"

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


def _exc_with_status(status_code: int) -> Exception:
    """Build a fake SDK error carrying a ``status_code`` attribute."""
    exc = Exception(f"HTTP {status_code}")
    exc.status_code = status_code  # type: ignore[attr-defined]
    return exc


@pytest.mark.unit
class TestIsRetryable:
    """_is_retryable classification: permanent 4xx vs transient errors."""

    @pytest.mark.parametrize(
        ("status_code", "expected"),
        [
            (400, False),
            (404, False),
            (408, True),
            (429, True),
            (500, True),
        ],
    )
    def test_status_codes(self, status_code: int, expected: bool) -> None:
        """SDK errors map to retryable/non-retryable per status code."""
        from workers.tasks.embed_fact import _is_retryable

        assert _is_retryable(_exc_with_status(status_code)) is expected

    def test_no_status_code_is_retryable(self) -> None:
        """Errors without a status code (timeouts, network) are retried."""
        from workers.tasks.embed_fact import _is_retryable

        assert _is_retryable(Exception("connection reset")) is True


@pytest.mark.unit
class TestEmbedFactRetryBehaviour:
    """Non-retryable 4xx retires the fact fast; transient errors retry."""

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
        }

    @staticmethod
    def _executed_sql(db: AsyncMock) -> list[str]:
        return [str(call.args[0]) for call in db.execute.call_args_list]

    @pytest.mark.asyncio
    async def test_non_retryable_400_retires_without_retry(
        self, embedder: AsyncMock
    ) -> None:
        """A 400 from the embedder retires the fact and raises immediately."""
        embedder.side_effect = _exc_with_status(400)

        with (
            patch("asyncio.sleep", new=AsyncMock()) as mock_sleep,
        ):
            db = self._make_db()
            ctx = self._ctx(db)

            from workers.tasks.embed_fact import embed_fact

            start = time.monotonic()
            with pytest.raises(Exception, match="HTTP 400"):
                await embed_fact(
                    ctx=ctx,
                    fact_id=_FACT_ID,
                    org_id=_ORG_ID,
                    content=_CONTENT,
                )
            elapsed = time.monotonic() - start

            # No retry loop: single attempt, no backoff sleep, fast return.
            embedder.assert_awaited_once()
            mock_sleep.assert_not_awaited()
            assert elapsed < 1.0
            # Fact retired: embedded_at set, embedding stays NULL.
            executed = self._executed_sql(db)
            assert any("embedded_at" in sql for sql in executed)
            assert not any("CAST(:embedding AS vector(768))" in sql for sql in executed)

    @pytest.mark.asyncio
    async def test_transient_error_retries_then_succeeds(
        self, embedder: AsyncMock
    ) -> None:
        """A transient failure is retried and a later success is stored."""
        embedder.side_effect = [Exception("connection reset"), [[0.2] * 768]]

        with (
            patch("asyncio.sleep", new=AsyncMock()) as mock_sleep,
        ):
            db = self._make_db()
            ctx = self._ctx(db)

            from workers.tasks.embed_fact import embed_fact

            await embed_fact(
                ctx=ctx,
                fact_id=_FACT_ID,
                org_id=_ORG_ID,
                content=_CONTENT,
            )

            assert embedder.await_count == 2
            mock_sleep.assert_awaited_once()
            executed = self._executed_sql(db)
            assert any("CAST(:embedding AS vector(768))" in sql for sql in executed)
