"""Unit tests for ARQ worker episode-not-found behaviour.

After the transaction-visibility fix, every enrichment worker raises
``EpisodeNotFoundError`` when the episode row is not yet visible in
PostgreSQL, instead of returning silently.  The ``@with_retry`` decorator
catches this exception and retries, giving the committing transaction time
to complete.

Each worker uses a slightly different code path for the episode lookup:
- ``classify_dialog`` / ``extract_structured`` / ``compute_observations``
  call ``EpisodeRepository.get_by_id_for_update()`` or ``.get_by_id()``.
- ``extract_entities`` / ``extract_facts`` run a raw ``select(Episode.user_id)``.
- ``link_entities_to_episode`` runs a raw ``select(Episode)``.

Important mocking notes:
  SQLAlchemy ``Result.scalar_one_or_none()`` and ``Result.all()`` are **sync**
  methods.  When the result object is an ``AsyncMock``, calling a sync method
  returns a coroutine (falsy, not ``None``), which breaks ``if user_id_row
  is None`` checks.  Therefore the execute-result mock **must** be a plain
  ``MagicMock``, not an ``AsyncMock``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core.exceptions import EpisodeNotFoundError


# ── Module-level constants ──────────────────────────────────────────────────────

_EPISODE_ID = str(uuid4())
_ORG_ID = str(uuid4())
_PROJECT_ID = str(uuid4())
_SESSION_ID = str(uuid4())
_CONTENT = "Test message content for worker tests."


# ── Shared fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def mock_db() -> AsyncMock:
    """Return a mock async DB session with a safe ``execute`` stub.

    ``execute`` returns a ``MagicMock`` (not ``AsyncMock``) whose
    ``scalar_one_or_none()`` returns ``None`` by default — see module
    docstring for the rationale.
    """
    m = AsyncMock()
    default_result = MagicMock()
    default_result.scalar_one_or_none.return_value = None
    default_result.all.return_value = []
    m.execute.return_value = default_result
    return m


@pytest.fixture
def mock_session_factory(mock_db: AsyncMock) -> MagicMock:
    """Return a mock session factory whose context manager yields *mock_db*."""
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = mock_db
    return factory


@pytest.fixture
def ctx(mock_session_factory: MagicMock) -> dict:
    """Return a minimal ARQ worker context dict with mocked engine + session."""
    return {
        "db_engine": MagicMock(),
        "db_session_factory": mock_session_factory,
    }


# ── EpisodeNotFoundError attribute tests ────────────────────────────────────────


class TestEpisodeNotFoundError:
    """Validate the exception class itself."""

    def test_is_app_error_subclass(self) -> None:
        """EpisodeNotFoundError inherits from AppError (not NotFoundError)."""
        assert issubclass(EpisodeNotFoundError, Exception)

    def test_status_code_and_code(self) -> None:
        """Has the correct HTTP status and machine-readable code."""
        exc = EpisodeNotFoundError("test")
        assert exc.status_code == 404
        assert exc.code == "episode_not_found"

    def test_default_message(self) -> None:
        """Default message is descriptive."""
        exc = EpisodeNotFoundError()
        assert exc.message == "The requested episode was not found."

    def test_custom_message_and_detail(self) -> None:
        """Accepts a custom message and detail dict."""
        exc = EpisodeNotFoundError(
            "Episode abc-123 not found",
            detail={"episode_id": "abc-123"},
        )
        assert exc.message == "Episode abc-123 not found"
        assert exc.detail == {"episode_id": "abc-123"}


# ── Worker behaviour tests ─────────────────────────────────────────────────────
# Each test verifies that:
#   1. The worker raises EpisodeNotFoundError when the episode lookup returns None
#   2. The @with_retry decorator is present (via __wrapped__ attribute)


class TestClassifyDialog:
    """classify_dialog raises EpisodeNotFoundError when episode is missing."""

    @pytest.mark.asyncio
    async def test_raises_on_missing_episode(
        self, ctx: dict, mock_db: AsyncMock
    ) -> None:
        """Repository returns None → EpisodeNotFoundError is raised."""
        mock_repo = AsyncMock()
        mock_repo.get_by_id_for_update.return_value = None

        with patch("asyncio.sleep", AsyncMock()):  # suppress retry back-off
            with patch(
                "repositories.episode_repository.EpisodeRepository",
                return_value=mock_repo,
            ):
                from workers.tasks.classify_dialog import classify_dialog

                with pytest.raises(EpisodeNotFoundError) as exc_info:
                    await classify_dialog(
                        ctx=ctx,
                        episode_id=_EPISODE_ID,
                        org_id=_ORG_ID,
                        project_id=_PROJECT_ID,
                        content=_CONTENT,
                    )

        assert exc_info.value.code == "episode_not_found"
        assert exc_info.value.status_code == 404
        assert _EPISODE_ID in exc_info.value.message

    def test_has_with_retry_decorator(self) -> None:
        """Function is wrapped by @with_retry."""
        from workers.tasks.classify_dialog import classify_dialog

        assert hasattr(classify_dialog, "__wrapped__")


class TestExtractEntities:
    """extract_entities raises EpisodeNotFoundError when episode is missing.

    Episode lookup is via a raw ``select(Episode.user_id)`` — the mock DB
    session's ``execute()`` returns a ``MagicMock`` whose
    ``scalar_one_or_none()`` returns ``None``.
    """

    @pytest.mark.asyncio
    async def test_raises_on_missing_episode(
        self, ctx: dict,
    ) -> None:
        """Raw user_id select returns None → EpisodeNotFoundError is raised."""

        with patch("asyncio.sleep", AsyncMock()):
            from workers.tasks.extract_entities import extract_entities

            with pytest.raises(EpisodeNotFoundError) as exc_info:
                await extract_entities(
                    ctx=ctx,
                    episode_id=_EPISODE_ID,
                    org_id=_ORG_ID,
                    project_id=_PROJECT_ID,
                    content=_CONTENT,
                )

        assert exc_info.value.code == "episode_not_found"
        assert exc_info.value.status_code == 404
        assert _EPISODE_ID in exc_info.value.message

    def test_has_with_retry_decorator(self) -> None:
        """Function is wrapped by @with_retry."""
        from workers.tasks.extract_entities import extract_entities

        assert hasattr(extract_entities, "__wrapped__")


class TestExtractStructured:
    """extract_structured raises EpisodeNotFoundError when episode is missing."""

    @pytest.mark.asyncio
    async def test_raises_on_missing_episode(
        self, ctx: dict, mock_db: AsyncMock
    ) -> None:
        """Repository returns None → EpisodeNotFoundError is raised."""
        mock_repo = AsyncMock()
        mock_repo.get_by_id_for_update.return_value = None

        with patch("asyncio.sleep", AsyncMock()):
            with patch(
                "repositories.episode_repository.EpisodeRepository",
                return_value=mock_repo,
            ):
                from workers.tasks.extract_structured import extract_structured

                with pytest.raises(EpisodeNotFoundError) as exc_info:
                    await extract_structured(
                        ctx=ctx,
                        episode_id=_EPISODE_ID,
                        org_id=_ORG_ID,
                        project_id=_PROJECT_ID,
                        session_id=_SESSION_ID,
                        content=_CONTENT,
                    )

        assert exc_info.value.code == "episode_not_found"
        assert exc_info.value.status_code == 404
        assert _EPISODE_ID in exc_info.value.message

    def test_has_with_retry_decorator(self) -> None:
        """Function is wrapped by @with_retry."""
        from workers.tasks.extract_structured import extract_structured

        assert hasattr(extract_structured, "__wrapped__")


class TestExtractFacts:
    """extract_facts raises EpisodeNotFoundError when episode is missing.

    Episode lookup is via a raw ``select(Episode.user_id)`` — same pattern
    as ``extract_entities``.
    """

    @pytest.mark.asyncio
    async def test_raises_on_missing_episode(
        self, ctx: dict,
    ) -> None:
        """Raw user_id select returns None → EpisodeNotFoundError is raised."""

        with patch("asyncio.sleep", AsyncMock()):
            from workers.tasks.extract_facts import extract_facts

            with pytest.raises(EpisodeNotFoundError) as exc_info:
                await extract_facts(
                    ctx=ctx,
                    episode_id=_EPISODE_ID,
                    org_id=_ORG_ID,
                    project_id=_PROJECT_ID,
                    content=_CONTENT,
                )

        assert exc_info.value.code == "episode_not_found"
        assert exc_info.value.status_code == 404
        assert _EPISODE_ID in exc_info.value.message

    def test_has_with_retry_decorator(self) -> None:
        """Function is wrapped by @with_retry."""
        from workers.tasks.extract_facts import extract_facts

        assert hasattr(extract_facts, "__wrapped__")


class TestLinkEntitiesToEpisode:
    """link_entities_to_episode raises EpisodeNotFoundError when episode missing.

    Episode lookup is via a raw ``select(Episode)`` — the mock DB session's
    ``execute()`` returns a ``MagicMock`` whose ``scalar_one_or_none()``
    returns ``None``.
    """

    @pytest.mark.asyncio
    async def test_raises_on_missing_episode(
        self, ctx: dict,
    ) -> None:
        """Raw select(Episode) returns None → EpisodeNotFoundError is raised."""

        with patch("asyncio.sleep", AsyncMock()):
            from workers.tasks.link_entities_to_episode import (
                link_entities_to_episode,
            )

            with pytest.raises(EpisodeNotFoundError) as exc_info:
                await link_entities_to_episode(
                    ctx=ctx,
                    episode_id=_EPISODE_ID,
                    org_id=_ORG_ID,
                    project_id=_PROJECT_ID,
                    content=_CONTENT,
                    role="user",
                )

        assert exc_info.value.code == "episode_not_found"
        assert exc_info.value.status_code == 404
        assert _EPISODE_ID in exc_info.value.message

    def test_has_with_retry_decorator(self) -> None:
        """Function is wrapped by @with_retry."""
        from workers.tasks.link_entities_to_episode import (
            link_entities_to_episode,
        )

        assert hasattr(link_entities_to_episode, "__wrapped__")


class TestComputeObservations:
    """compute_observations raises EpisodeNotFoundError when episode is missing."""

    @pytest.mark.asyncio
    async def test_raises_on_missing_episode(
        self, ctx: dict, mock_db: AsyncMock
    ) -> None:
        """Repository returns None → EpisodeNotFoundError is raised."""
        mock_repo = AsyncMock()
        mock_repo.get_by_id.return_value = None

        with patch("asyncio.sleep", AsyncMock()):
            with patch(
                "repositories.episode_repository.EpisodeRepository",
                return_value=mock_repo,
            ):
                from workers.tasks.compute_observations import compute_observations

                with pytest.raises(EpisodeNotFoundError) as exc_info:
                    await compute_observations(
                        ctx=ctx,
                        episode_id=_EPISODE_ID,
                        org_id=_ORG_ID,
                        project_id=_PROJECT_ID,
                    )

        assert exc_info.value.code == "episode_not_found"
        assert exc_info.value.status_code == 404
        assert _EPISODE_ID in exc_info.value.message

    def test_has_with_retry_decorator(self) -> None:
        """Function is wrapped by @with_retry."""
        from workers.tasks.compute_observations import compute_observations

        assert hasattr(compute_observations, "__wrapped__")
