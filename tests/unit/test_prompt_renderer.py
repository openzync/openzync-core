"""Unit tests for prompt_renderer — auto-injection, static registry, providers.

Tests cover:
- Basic rendering with explicit kwargs (eval-test path, no auto-injection).
- TYPE_DATA_SOURCES registry completeness and correctness.
- Auto-injection path with mocked DB session (each provider).
- Edge cases (missing template_text, unknown type, partial identifiers).
- return_context path.
- resolve_prompt_template_by_type (lightweight, mocked).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from uuid import UUID, uuid4

import pytest
from pytest import MonkeyPatch

from services.worker.prompt_renderer import (
    TYPE_DATA_SOURCES,
    DataSource,
    render_prompt,
    resolve_prompt_template_by_type,
)


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def org_id() -> UUID:
    return uuid4()


@pytest.fixture
def session_id() -> UUID:
    return uuid4()


@pytest.fixture
def episode_id() -> UUID:
    return uuid4()


@pytest.fixture
def user_id() -> UUID:
    return uuid4()


class MockResult:
    """Mimics ``CursorResult`` / ``ScalarResult`` for async session execute()."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def scalars(self) -> MockResult:
        return self

    def all(self) -> list[Any]:
        return self._rows

    def fetchall(self) -> list[Any]:
        return self._rows

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class FakeAsyncSession:
    """Minimal proxy that acts like an ``AsyncSession`` for ``async with``."""

    def __init__(self, execute_result: list[Any] | None = None) -> None:
        self._execute_result = execute_result or []

    async def __aenter__(self) -> FakeAsyncSession:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    async def execute(self, query: Any, params: Any | None = None) -> MockResult:
        return MockResult(self._execute_result)

    def add(self, instance: Any) -> None:
        pass

    async def flush(self) -> None:
        pass

    async def refresh(self, instance: Any) -> None:
        pass


class _FakeSessionFactory:
    """Callable that mimics ``async_sessionmaker``.

    ``__call__`` is sync — ``async_sessionmaker()`` returns an ``AsyncSession``
    synchronously, then ``async with`` invokes the session's async context manager
    protocol (``__aenter__`` / ``__aexit__``).
    """

    def __init__(self, execute_result: list[Any] | None = None) -> None:
        self._execute_result = execute_result or []

    def __call__(self) -> FakeAsyncSession:
        return FakeAsyncSession(self._execute_result)


def make_fake_session_factory(
    execute_result: list[Any] | None = None,
) -> _FakeSessionFactory:
    """Return a callable that behaves like ``async_sessionmaker``."""
    return _FakeSessionFactory(execute_result)


@pytest.fixture
def mock_db_session_factory(org_id: UUID) -> Any:
    """Return a factory that behaves like ``async_sessionmaker``.

    Tests that need custom DB responses should instead monkeypatch
    individual provider functions in ``_PROVIDER_DISPATCH``.
    """
    return make_fake_session_factory()


# ══════════════════════════════════════════════════════════════════════════════
# TYPE_DATA_SOURCES registry tests
# ══════════════════════════════════════════════════════════════════════════════

class TestTypeDataSourceRegistry:
    """The static registry must cover all 5 types and known variables."""

    def test_all_five_types_are_registered(self) -> None:
        assert set(TYPE_DATA_SOURCES.keys()) == {
            "fact_extraction",
            "entity_extraction",
            "classification",
            "structured_extraction",
            "user_summary",
        }

    def test_fact_extraction_sources(self) -> None:
        sources = TYPE_DATA_SOURCES["fact_extraction"]
        assert DataSource.EPISODE_CONTENT in sources
        assert DataSource.SESSION_ENTITIES in sources
        assert DataSource.SESSION_FACTS in sources
        assert DataSource.SESSION_RECENT_HISTORY in sources
        assert len(sources) == 4

    def test_entity_extraction_sources(self) -> None:
        sources = TYPE_DATA_SOURCES["entity_extraction"]
        assert DataSource.EPISODE_CONTENT in sources
        assert DataSource.SESSION_ENTITIES in sources
        assert DataSource.ORG_ENTITY_TYPES in sources
        assert len(sources) == 3

    def test_classification_sources(self) -> None:
        sources = TYPE_DATA_SOURCES["classification"]
        assert DataSource.EPISODE_CONTENT in sources
        assert DataSource.ORG_CLASSIFICATION_LABELS in sources
        assert len(sources) == 2

    def test_structured_extraction_sources(self) -> None:
        sources = TYPE_DATA_SOURCES["structured_extraction"]
        assert DataSource.EPISODE_CONTENT in sources
        assert DataSource.ORG_STRUCTURED_SCHEMAS in sources
        assert len(sources) == 2

    def test_user_summary_sources(self) -> None:
        sources = TYPE_DATA_SOURCES["user_summary"]
        assert DataSource.USER_EPISODES in sources
        assert DataSource.USER_FACTS in sources
        assert DataSource.USER_ENTITIES in sources
        assert DataSource.USER_CLASSIFICATIONS in sources
        assert DataSource.CUSTOM_INSTRUCTIONS in sources
        assert len(sources) == 5

    def test_every_datasource_has_provider(self) -> None:
        """Every DataSource enum member must have a provider in _PROVIDER_DISPATCH."""
        from services.worker.prompt_renderer import _PROVIDER_DISPATCH

        defined_sources = set(DataSource)
        dispatched_sources = set(_PROVIDER_DISPATCH.keys())
        assert defined_sources == dispatched_sources, (
            f"Missing providers for: {defined_sources - dispatched_sources}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Basic rendering (eval-test path, no auto-injection)
# ══════════════════════════════════════════════════════════════════════════════

class TestBasicRender:
    """render_prompt with explicit kwargs and template_text — no DB needed."""

    @pytest.mark.asyncio
    async def test_simple_template_renders_correctly(self) -> None:
        prompt = await render_prompt(
            "test_type",
            template_text="Hello {{ name }}!",
            name="World",
        )
        assert prompt == "Hello World!"

    @pytest.mark.asyncio
    async def test_template_with_multiple_variables(self) -> None:
        prompt = await render_prompt(
            "test_type",
            template_text="{{ a }} + {{ b }} = {{ c }}",
            a=1,
            b=2,
            c=3,
        )
        assert prompt == "1 + 2 = 3"

    @pytest.mark.asyncio
    async def test_template_with_jinja_conditional(self) -> None:
        prompt = await render_prompt(
            "test_type",
            template_text="{% if show %}VISIBLE{% else %}HIDDEN{% endif %}",
            show=True,
        )
        assert prompt == "VISIBLE"

    @pytest.mark.asyncio
    async def test_template_with_loop(self) -> None:
        prompt = await render_prompt(
            "test_type",
            template_text="{% for x in items %}{{ x }},{% endfor %}",
            items=["a", "b", "c"],
        )
        assert prompt == "a,b,c,"

    @pytest.mark.asyncio
    async def test_raises_value_error_without_template_text_and_org_id(self) -> None:
        with pytest.raises(ValueError, match="No template_text"):
            await render_prompt("fact_extraction")

    @pytest.mark.asyncio
    async def test_extra_context_overrides_nothing_when_no_auto_inject(self) -> None:
        """When org_id is None, extra_context is the only context."""
        prompt = await render_prompt(
            "test_type",
            template_text="{{ key }}",
            key="explicit",
        )
        assert prompt == "explicit"


# ══════════════════════════════════════════════════════════════════════════════
# Auto-injection path (mocked DB)
# ══════════════════════════════════════════════════════════════════════════════

class TestAutoInjection:
    """render_prompt with org_id + db_session_factory — uses providers."""

    @pytest.mark.asyncio
    async def test_unknown_prompt_type_raises_key_error(
        self,
        org_id: UUID,
        mock_db_session_factory: Any,
    ) -> None:
        with pytest.raises(KeyError, match="Unknown prompt type"):
            await render_prompt(
                "nonexistent_type",
                org_id=org_id,
                db_session_factory=mock_db_session_factory,
                template_text="ignored",
            )

    @staticmethod
    def _make_provider_side_effect(
        var_name: str,
        value: Any,
    ) -> Any:
        """Return a provider function that yields ``{var_name: value}``."""

        async def provider(**kwargs: Any) -> dict[str, Any]:
            return {var_name: value}

        return provider

    @pytest.mark.asyncio
    async def test_fact_extraction_injects_conversation(
        self,
        org_id: UUID,
        episode_id: UUID,
        session_id: UUID,
        monkeypatch: MonkeyPatch,
    ) -> None:
        """Override EPISODE_CONTENT provider to return known conversation."""
        from services.worker.prompt_renderer import (
            _PROVIDER_DISPATCH,
            DataSource,
        )

        async def mock_provider(**kwargs: Any) -> dict[str, Any]:
            return {"conversation": "Hello from mock"}

        monkeypatch.setitem(
            _PROVIDER_DISPATCH,
            DataSource.EPISODE_CONTENT,
            mock_provider,
        )
        monkeypatch.setitem(
            _PROVIDER_DISPATCH,
            DataSource.SESSION_ENTITIES,
            self._make_provider_side_effect("known_entities", []),
        )
        monkeypatch.setitem(
            _PROVIDER_DISPATCH,
            DataSource.SESSION_FACTS,
            self._make_provider_side_effect("existing_facts", []),
        )
        monkeypatch.setitem(
            _PROVIDER_DISPATCH,
            DataSource.SESSION_RECENT_HISTORY,
            self._make_provider_side_effect("recent_history", []),
        )

        prompt = await render_prompt(
            "fact_extraction",
            org_id=org_id,
            episode_id=episode_id,
            session_id=session_id,
            db_session_factory=make_fake_session_factory(),
            template_text="Extract: {{ conversation }}",
        )
        assert "Hello from mock" in prompt
        assert prompt == "Extract: Hello from mock"

    @pytest.mark.asyncio
    async def test_extra_context_overrides_auto_injected(
        self,
        org_id: UUID,
        monkeypatch: MonkeyPatch,
    ) -> None:
        """Caller-provided extra_context takes precedence over injected values."""
        from services.worker.prompt_renderer import (
            _PROVIDER_DISPATCH,
            DataSource,
        )

        async def mock_content(**kwargs: Any) -> dict[str, Any]:
            return {"conversation": "from_db"}

        monkeypatch.setitem(
            _PROVIDER_DISPATCH,
            DataSource.EPISODE_CONTENT,
            mock_content,
        )
        monkeypatch.setitem(
            _PROVIDER_DISPATCH,
            DataSource.SESSION_ENTITIES,
            self._make_provider_side_effect("known_entities", []),
        )
        monkeypatch.setitem(
            _PROVIDER_DISPATCH,
            DataSource.SESSION_FACTS,
            self._make_provider_side_effect("existing_facts", []),
        )
        monkeypatch.setitem(
            _PROVIDER_DISPATCH,
            DataSource.SESSION_RECENT_HISTORY,
            self._make_provider_side_effect("recent_history", []),
        )

        prompt = await render_prompt(
            "fact_extraction",
            org_id=org_id,
            db_session_factory=make_fake_session_factory(),
            template_text="Data: {{ conversation }}",
            conversation="from_caller",
        )
        assert prompt == "Data: from_caller"

    @pytest.mark.asyncio
    async def test_return_context_returns_tuple(
        self,
        org_id: UUID,
        monkeypatch: MonkeyPatch,
    ) -> None:
        """When return_context=True, both prompt string and context dict are returned."""
        from services.worker.prompt_renderer import (
            _PROVIDER_DISPATCH,
            DataSource,
        )

        async def mock_provider(**kwargs: Any) -> dict[str, Any]:
            return {"conversation": "ctx_data"}

        monkeypatch.setitem(
            _PROVIDER_DISPATCH,
            DataSource.EPISODE_CONTENT,
            mock_provider,
        )
        monkeypatch.setitem(
            _PROVIDER_DISPATCH,
            DataSource.SESSION_ENTITIES,
            self._make_provider_side_effect("known_entities", ["e1", "e2"]),
        )
        monkeypatch.setitem(
            _PROVIDER_DISPATCH,
            DataSource.SESSION_FACTS,
            self._make_provider_side_effect("existing_facts", [{"f1": 1}]),
        )
        monkeypatch.setitem(
            _PROVIDER_DISPATCH,
            DataSource.SESSION_RECENT_HISTORY,
            self._make_provider_side_effect("recent_history", []),
        )

        result = await render_prompt(
            "fact_extraction",
            org_id=org_id,
            episode_id=uuid4(),
            session_id=uuid4(),
            db_session_factory=make_fake_session_factory(),
            template_text="Test: {{ conversation }}",
            return_context=True,
        )
        assert isinstance(result, tuple)
        prompt_str, context = result
        assert prompt_str == "Test: ctx_data"
        assert "known_entities" in context
        assert context["known_entities"] == ["e1", "e2"]
        assert "existing_facts" in context
        assert context["existing_facts"] == [{"f1": 1}]

    @pytest.mark.asyncio
    async def test_injects_missing_org_id_no_auto_injection(
        self,
    ) -> None:
        """Without org_id, no providers are called — only extra_context renders."""
        prompt = await render_prompt(
            "fact_extraction",
            template_text="{{ conversation }}",
            conversation="no_db_needed",
        )
        assert prompt == "no_db_needed"


# ══════════════════════════════════════════════════════════════════════════════
# user_summary computed variable (episode_count)
# ══════════════════════════════════════════════════════════════════════════════

class TestUserSummaryComputed:
    """user_summary type auto-computes episode_count from episodes list."""

    @pytest.mark.asyncio
    async def test_episode_count_is_computed(
        self,
        org_id: UUID,
        user_id: UUID,
        monkeypatch: MonkeyPatch,
    ) -> None:
        from services.worker.prompt_renderer import (
            _PROVIDER_DISPATCH,
            DataSource,
        )

        async def mock_episodes(**kwargs: Any) -> dict[str, Any]:
            return {"episodes": [{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello"}]}

        async def mock_provider(**kwargs: Any) -> dict[str, Any]:
            return {}

        # Only USER_EPISODES returns data; all others return empty
        monkeypatch.setitem(
            _PROVIDER_DISPATCH,
            DataSource.USER_EPISODES,
            mock_episodes,
        )
        for source in (DataSource.USER_FACTS, DataSource.USER_ENTITIES,
                       DataSource.USER_CLASSIFICATIONS, DataSource.CUSTOM_INSTRUCTIONS):
            monkeypatch.setitem(_PROVIDER_DISPATCH, source, mock_provider)

        prompt, ctx = await render_prompt(
            "user_summary",
            org_id=org_id,
            user_id=user_id,
            db_session_factory=make_fake_session_factory(),
            template_text="Count: {{ episode_count }}",
            return_context=True,
        )
        assert ctx["episode_count"] == 2
        assert prompt == "Count: 2"


# ══════════════════════════════════════════════════════════════════════════════
# resolve_prompt_template_by_type
# ══════════════════════════════════════════════════════════════════════════════

class TestResolveByType:
    """Lightweight tests for resolve_prompt_template_by_type."""

    @pytest.mark.asyncio
    async def test_returns_none_when_no_active_template(
        self,
        org_id: UUID,
        monkeypatch: MonkeyPatch,
    ) -> None:
        """When the DB returns nothing, the function returns None."""
        from repositories.prompt_template_repository import (
            PromptTemplateRepository,
        )

        class MockRepo:
            async def get_active_by_type(self, **kwargs: Any) -> None:
                return None

        monkeypatch.setattr(
            PromptTemplateRepository,
            "get_active_by_type",
            MockRepo().get_active_by_type,
        )

        result = await resolve_prompt_template_by_type(
            "fact_extraction",
            org_id,
            make_fake_session_factory(),
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_accepts_string_org_id(
        self,
        monkeypatch: MonkeyPatch,
    ) -> None:
        """String org_id is accepted and converted to UUID internally."""
        from repositories.prompt_template_repository import (
            PromptTemplateRepository,
        )

        class MockRepo:
            async def get_active_by_type(self, **kwargs: Any) -> None:
                return None

        monkeypatch.setattr(
            PromptTemplateRepository,
            "get_active_by_type",
            MockRepo().get_active_by_type,
        )

        result = await resolve_prompt_template_by_type(
            "classification",
            str(uuid4()),
            make_fake_session_factory(),
        )
        assert result is None
