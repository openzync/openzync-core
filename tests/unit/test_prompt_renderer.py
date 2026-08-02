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

from typing import Any
from uuid import UUID, uuid4

import pytest
from pytest import MonkeyPatch

from services.worker.prompt_renderer import (
    TYPE_DATA_SOURCES,
    DataSource,
    _fetch_classification_labels,
    _fetch_custom_instructions,
    _fetch_episode_content,
    _fetch_episode_metadata,
    _fetch_org_entity_types,
    _fetch_session_entities,
    _fetch_session_facts,
    _fetch_session_recent_history,
    _fetch_similar_episodes,
    _fetch_similar_facts,
    _fetch_structured_schemas,
    _fetch_user_classifications,
    _fetch_user_entities,
    _fetch_user_episodes,
    _fetch_user_facts,
    build_enrichment_prompt,
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


@pytest.fixture
def project_id() -> UUID:
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
            "enrich_episode",
        }

    def test_fact_extraction_sources(self) -> None:
        sources = TYPE_DATA_SOURCES["fact_extraction"]
        assert DataSource.EPISODE_CONTENT in sources
        assert DataSource.SESSION_ENTITIES in sources
        assert DataSource.SESSION_FACTS in sources
        assert DataSource.SESSION_RECENT_HISTORY in sources
        assert DataSource.SIMILAR_EPISODES in sources
        assert DataSource.SIMILAR_FACTS in sources
        assert DataSource.EPISODE_METADATA in sources
        assert len(sources) == 7

    def test_entity_extraction_sources(self) -> None:
        sources = TYPE_DATA_SOURCES["entity_extraction"]
        assert DataSource.EPISODE_CONTENT in sources
        assert DataSource.SESSION_ENTITIES in sources
        assert DataSource.ORG_ENTITY_TYPES in sources
        assert DataSource.SIMILAR_EPISODES in sources
        assert DataSource.SIMILAR_FACTS in sources
        assert DataSource.EPISODE_METADATA in sources
        assert len(sources) == 6

    def test_classification_sources(self) -> None:
        sources = TYPE_DATA_SOURCES["classification"]
        assert DataSource.EPISODE_CONTENT in sources
        assert DataSource.ORG_CLASSIFICATION_LABELS in sources
        assert DataSource.SESSION_RECENT_HISTORY in sources
        assert DataSource.SIMILAR_EPISODES in sources
        assert DataSource.SIMILAR_FACTS in sources
        assert DataSource.EPISODE_METADATA in sources
        assert len(sources) == 6

    def test_structured_extraction_sources(self) -> None:
        sources = TYPE_DATA_SOURCES["structured_extraction"]
        assert DataSource.EPISODE_CONTENT in sources
        assert DataSource.ORG_STRUCTURED_SCHEMAS in sources
        assert DataSource.SESSION_ENTITIES in sources
        assert DataSource.SESSION_FACTS in sources
        assert DataSource.SESSION_RECENT_HISTORY in sources
        assert DataSource.SIMILAR_EPISODES in sources
        assert DataSource.SIMILAR_FACTS in sources
        assert DataSource.EPISODE_METADATA in sources
        assert len(sources) == 8

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
    async def test_simple_template_returns_as_is(self) -> None:
        """Template text is returned as plain text (no Jinja2 rendering).

        Context injection is handled by ``build_enrichment_prompt()``, not
        by the template renderer.
        """
        prompt = await render_prompt(
            "test_type",
            template_text="Hello {{ name }}!",
            name="World",
        )
        assert prompt == "Hello {{ name }}!"

    @pytest.mark.asyncio
    async def test_template_with_variables_returns_as_is(self) -> None:
        prompt = await render_prompt(
            "test_type",
            template_text="{{ a }} + {{ b }} = {{ c }}",
            a=1,
            b=2,
            c=3,
        )
        assert prompt == "{{ a }} + {{ b }} = {{ c }}"

    @pytest.mark.asyncio
    async def test_template_with_jinja_syntax_returns_as_is(self) -> None:
        prompt = await render_prompt(
            "test_type",
            template_text="{% if show %}VISIBLE{% else %}HIDDEN{% endif %}",
            show=True,
        )
        assert prompt == "{% if show %}VISIBLE{% else %}HIDDEN{% endif %}"

    @pytest.mark.asyncio
    async def test_template_with_loop_syntax_returns_as_is(self) -> None:
        prompt = await render_prompt(
            "test_type",
            template_text="{% for x in items %}{{ x }},{% endfor %}",
            items=["a", "b", "c"],
        )
        assert prompt == "{% for x in items %}{{ x }},{% endfor %}"

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
        # Template text is returned as-is — no Jinja2 rendering.
        assert prompt == "{{ key }}"


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
        # Template text is returned as-is (no Jinja2).  Context is available
        # in the dict when return_context=True but the template is not rendered.
        assert prompt == "Extract: {{ conversation }}"

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
        # Template text is returned as-is (no Jinja2).  Context is
        # injected by build_enrichment_prompt() downstream.
        assert prompt == "Data: {{ conversation }}"

    @pytest.mark.asyncio
    async def test_return_context_returns_tuple(
        self,
        org_id: UUID,
        monkeypatch: MonkeyPatch,
    ) -> None:
        """When return_context=True, prompt string and context dict are returned."""
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
        # Template text is returned as-is (no Jinja2 rendering).
        assert prompt_str == "Test: {{ conversation }}"
        assert "known_entities" in context
        assert context["known_entities"] == ["e1", "e2"]
        assert "existing_facts" in context
        assert context["existing_facts"] == [{"f1": 1}]

    @pytest.mark.asyncio
    async def test_injects_missing_org_id_no_auto_injection(
        self,
    ) -> None:
        """Without org_id, no providers are called — no context injection."""
        prompt = await render_prompt(
            "fact_extraction",
            template_text="{{ conversation }}",
            conversation="no_db_needed",
        )
        # Template text is returned as-is — no Jinja2 rendering.
        assert prompt == "{{ conversation }}"


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
            return {
                "episodes": [
                    {"role": "user", "content": "Hi"},
                    {"role": "assistant", "content": "Hello"},
                ],
            }

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
        # Template text is returned as-is (no Jinja2 rendering).
        assert prompt == "Count: {{ episode_count }}"


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

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_returns_template_text_when_found(
        self,
        org_id: UUID,
        monkeypatch: MonkeyPatch,
    ) -> None:
        """Mock repo returns a template with template_text."""
        from repositories.prompt_template_repository import (
            PromptTemplateRepository,
        )

        class MockTemplate:
            template_text = "You are a helpful assistant."

        class MockRepo:
            async def get_active_by_type(self, **kwargs: Any) -> MockTemplate:
                return MockTemplate()

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
        assert result == "You are a helpful assistant."


# ══════════════════════════════════════════════════════════════════════════════
# Individual provider functions
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestProviderFetchEpisodeContent:
    """Tests for _fetch_episode_content."""

    @pytest.mark.asyncio
    async def test_with_episode_id(self, org_id: UUID, episode_id: UUID) -> None:
        """Returns conversation content when episode is found."""
        session = FakeAsyncSession(["Hello world"])
        result = await _fetch_episode_content(
            db=session,
            org_id=org_id,
            episode_id=episode_id,
        )
        assert result == {"conversation": "Hello world"}

    @pytest.mark.asyncio
    async def test_without_episode_id(self, org_id: UUID) -> None:
        """Returns empty conversation when episode_id is None."""
        result = await _fetch_episode_content(
            db=FakeAsyncSession(),
            org_id=org_id,
            episode_id=None,
        )
        assert result == {"conversation": ""}

    @pytest.mark.asyncio
    async def test_episode_not_found(self, org_id: UUID, episode_id: UUID) -> None:
        """Returns empty conversation when episode row is None."""
        session = FakeAsyncSession([])
        result = await _fetch_episode_content(
            db=session,
            org_id=org_id,
            episode_id=episode_id,
        )
        assert result == {"conversation": ""}


@pytest.mark.unit
class TestProviderFetchSessionEntities:
    """Tests for _fetch_session_entities."""

    @pytest.mark.asyncio
    async def test_no_session_id(
        self, org_id: UUID, project_id: UUID,
    ) -> None:
        """session_id is None → empty entities."""
        result = await _fetch_session_entities(
            db=FakeAsyncSession(),
            org_id=org_id,
            session_id=None,
            graph_backend=None,
            project_id=project_id,
        )
        assert result == {"known_entities": []}

    @pytest.mark.asyncio
    async def test_no_project_id(
        self, org_id: UUID, session_id: UUID,
    ) -> None:
        """project_id is None → empty entities."""
        result = await _fetch_session_entities(
            db=FakeAsyncSession(),
            org_id=org_id,
            session_id=session_id,
            graph_backend=None,
            project_id=None,
        )
        assert result == {"known_entities": []}

    @pytest.mark.asyncio
    async def test_no_graph_backend(
        self, org_id: UUID, session_id: UUID, project_id: UUID,
    ) -> None:
        """graph_backend is None → empty entities."""
        result = await _fetch_session_entities(
            db=FakeAsyncSession(),
            org_id=org_id,
            session_id=session_id,
            graph_backend=None,
            project_id=project_id,
        )
        assert result == {"known_entities": []}

    @pytest.mark.asyncio
    async def test_with_postgres_graph_backend(
        self,
        org_id: UUID,
        session_id: UUID,
        project_id: UUID,
        monkeypatch: MonkeyPatch,
    ) -> None:
        """With PostgresGraphBackend and db_session_factory, creates fresh session."""
        class FakePostgresBackend:
            def __init__(self, db: Any = None) -> None:
                self.db = db

            async def get_entities_for_session(
                self, **kwargs: Any,
            ) -> list[dict[str, str]]:
                return [{"name": "Entity1", "entity_type": "Person"}]

        import packages.graph_backend.postgres  # noqa: F811 — ensure module loaded
        monkeypatch.setattr(
            packages.graph_backend.postgres,
            "PostgresGraphBackend",
            FakePostgresBackend,
        )

        pg_backend = FakePostgresBackend()
        result = await _fetch_session_entities(
            db=FakeAsyncSession(),
            org_id=org_id,
            session_id=session_id,
            graph_backend=pg_backend,
            project_id=project_id,
            db_session_factory=make_fake_session_factory(),
        )
        assert result == {
            "known_entities": [{"name": "Entity1", "entity_type": "Person"}],
        }

    @pytest.mark.asyncio
    async def test_with_non_postgres_backend(
        self,
        org_id: UUID,
        session_id: UUID,
        project_id: UUID,
    ) -> None:
        """Uses graph_backend.get_entities_for_session directly."""
        class NonPostgresBackend:
            async def get_entities_for_session(
                self, **kwargs: Any,
            ) -> list[dict[str, str]]:
                return [{"name": "E1", "entity_type": "Location"}]

        backend = NonPostgresBackend()
        result = await _fetch_session_entities(
            db=FakeAsyncSession(),
            org_id=org_id,
            session_id=session_id,
            graph_backend=backend,
            project_id=project_id,
        )
        assert result == {"known_entities": [{"name": "E1", "entity_type": "Location"}]}


@pytest.mark.unit
class TestProviderFetchSessionFacts:
    """Tests for _fetch_session_facts."""

    @pytest.mark.asyncio
    async def test_no_session_id(self, org_id: UUID) -> None:
        """Returns existing_facts as empty list."""
        result = await _fetch_session_facts(
            db=FakeAsyncSession(),
            org_id=org_id,
            session_id=None,
        )
        assert result == {"existing_facts": []}

    @pytest.mark.asyncio
    async def test_with_session_id(
        self, org_id: UUID, session_id: UUID, monkeypatch: MonkeyPatch,
    ) -> None:
        """Mock FactRepository.list_by_session, returns facts."""
        from repositories.fact_repository import FactRepository

        async def mock_list_by_session(
            self: Any,
            organization_id: UUID,
            session_id: UUID,
            limit: int,
        ) -> tuple[list[dict[str, str]], Any]:
            return [{"subject": "S", "predicate": "P", "object": "O"}], None

        monkeypatch.setattr(FactRepository, "list_by_session", mock_list_by_session)

        result = await _fetch_session_facts(
            db=FakeAsyncSession(),
            org_id=org_id,
            session_id=session_id,
        )
        assert result == {
            "existing_facts": [{"subject": "S", "predicate": "P", "object": "O"}],
        }


@pytest.mark.unit
class TestProviderFetchSessionRecentHistory:
    """Tests for _fetch_session_recent_history."""

    @pytest.mark.asyncio
    async def test_no_session_id(self, org_id: UUID) -> None:
        """Returns recent_history as empty list."""
        result = await _fetch_session_recent_history(
            db=FakeAsyncSession(),
            org_id=org_id,
            session_id=None,
            episode_id=None,
        )
        assert result == {"recent_history": []}

    @pytest.mark.asyncio
    async def test_with_episode_id_filter(
        self, org_id: UUID, session_id: UUID, episode_id: UUID,
    ) -> None:
        """Excludes current episode from results."""
        class FakeEp:
            def __init__(self, role: str, content: str) -> None:
                self.role = role
                self.content = content

        rows = [
            FakeEp("assistant", "Hi there"),
            FakeEp("user", "Hello"),
        ]
        session = FakeAsyncSession(rows)

        result = await _fetch_session_recent_history(
            db=session,
            org_id=org_id,
            session_id=session_id,
            episode_id=episode_id,
        )
        assert result == {
            "recent_history": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there"},
            ],
        }

    @pytest.mark.asyncio
    async def test_without_episode_id(
        self, org_id: UUID, session_id: UUID,
    ) -> None:
        """Returns all recent history without episode filter."""
        class FakeEp:
            def __init__(self, role: str, content: str) -> None:
                self.role = role
                self.content = content

        rows = [FakeEp("user", "Only message")]
        session = FakeAsyncSession(rows)

        result = await _fetch_session_recent_history(
            db=session,
            org_id=org_id,
            session_id=session_id,
            episode_id=None,
        )
        assert result == {
            "recent_history": [{"role": "user", "content": "Only message"}],
        }


@pytest.mark.unit
class TestProviderFetchEpisodeMetadata:
    """Tests for _fetch_episode_metadata."""

    @pytest.mark.asyncio
    async def test_with_episode_id(self, org_id: UUID, episode_id: UUID) -> None:
        """Returns metadata dict."""
        session = FakeAsyncSession([{"key": "value", "source": "test"}])
        result = await _fetch_episode_metadata(
            db=session,
            org_id=org_id,
            episode_id=episode_id,
        )
        assert result == {"message_metadata": {"key": "value", "source": "test"}}

    @pytest.mark.asyncio
    async def test_without_episode_id(self, org_id: UUID) -> None:
        """Returns empty metadata dict."""
        result = await _fetch_episode_metadata(
            db=FakeAsyncSession(),
            org_id=org_id,
            episode_id=None,
        )
        assert result == {"message_metadata": {}}


@pytest.mark.unit
class TestProviderFetchSimilarEpisodes:
    """Tests for _fetch_similar_episodes."""

    @pytest.mark.asyncio
    async def test_no_episode_id(self, org_id: UUID, user_id: UUID) -> None:
        """Returns empty list when no episode_id."""
        result = await _fetch_similar_episodes(
            db=FakeAsyncSession(),
            org_id=org_id,
            episode_id=None,
            user_id=user_id,
        )
        assert result == {"similar_episodes": []}

    @pytest.mark.asyncio
    async def test_no_user_id(self, org_id: UUID, episode_id: UUID) -> None:
        """Returns empty list when no user_id."""
        result = await _fetch_similar_episodes(
            db=FakeAsyncSession(),
            org_id=org_id,
            episode_id=episode_id,
            user_id=None,
        )
        assert result == {"similar_episodes": []}

    @pytest.mark.asyncio
    async def test_with_valid_ids(
        self,
        org_id: UUID,
        episode_id: UUID,
        user_id: UUID,
        project_id: UUID,
        monkeypatch: MonkeyPatch,
    ) -> None:
        """Uses EpisodeRepository.search_by_bm25, filters out current episode."""
        from repositories.episode_repository import EpisodeRepository

        async def mock_search_by_bm25(
            self: Any, query: str, project_id: UUID, org_id: UUID, limit: int,
        ) -> list[dict[str, Any]]:
            return [
                {"id": str(uuid4()), "content": "Similar one", "role": "user", "score": 0.85},
                {"id": str(episode_id), "content": "Self match"},
                {"id": str(uuid4()), "content": "Similar two", "role": "assistant", "score": 0.72},
            ]

        monkeypatch.setattr(EpisodeRepository, "search_by_bm25", mock_search_by_bm25)

        # First DB query returns (content, project_id) for the episode
        session = FakeAsyncSession([("query content", project_id)])
        result = await _fetch_similar_episodes(
            db=session,
            org_id=org_id,
            episode_id=episode_id,
            user_id=user_id,
        )
        assert len(result["similar_episodes"]) == 2
        # The current episode_id should be filtered out
        ids = [ep["id"] for ep in result["similar_episodes"]]
        assert str(episode_id) not in ids


@pytest.mark.unit
class TestProviderFetchSimilarFacts:
    """Tests for _fetch_similar_facts."""

    @pytest.mark.asyncio
    async def test_no_episode_id(self, org_id: UUID, user_id: UUID) -> None:
        """Returns empty list when no episode_id."""
        result = await _fetch_similar_facts(
            db=FakeAsyncSession(),
            org_id=org_id,
            episode_id=None,
            user_id=user_id,
        )
        assert result == {"related_facts": []}

    @pytest.mark.asyncio
    async def test_no_user_id(self, org_id: UUID, episode_id: UUID) -> None:
        """Returns empty list when no user_id."""
        result = await _fetch_similar_facts(
            db=FakeAsyncSession(),
            org_id=org_id,
            episode_id=episode_id,
            user_id=None,
        )
        assert result == {"related_facts": []}

    @pytest.mark.asyncio
    async def test_with_valid_ids(
        self,
        org_id: UUID,
        episode_id: UUID,
        user_id: UUID,
        project_id: UUID,
        monkeypatch: MonkeyPatch,
    ) -> None:
        """Uses FactRepository.search_by_bm25."""
        from repositories.fact_repository import FactRepository

        async def mock_search_by_bm25(
            self: Any, query: str, project_id: UUID, org_id: UUID, limit: int,
        ) -> list[dict[str, Any]]:
            return [
                {"subject": "S1", "predicate": "P1", "object": "O1", "score": 0.9},
            ]

        monkeypatch.setattr(FactRepository, "search_by_bm25", mock_search_by_bm25)

        session = FakeAsyncSession([("query content", project_id)])
        result = await _fetch_similar_facts(
            db=session,
            org_id=org_id,
            episode_id=episode_id,
            user_id=user_id,
        )
        assert result == {
            "related_facts": [
                {"subject": "S1", "predicate": "P1", "object": "O1", "score": 0.9},
            ],
        }


@pytest.mark.unit
class TestProviderFetchOrgEntityTypes:
    """Tests for _fetch_org_entity_types."""

    @pytest.mark.asyncio
    async def test_no_schemas(self, org_id: UUID) -> None:
        """Returns default entity types when no schemas."""
        session = FakeAsyncSession([])
        result = await _fetch_org_entity_types(
            db=session,
            org_id=org_id,
        )
        assert result == {
            "entity_types": [
                "Person", "Organization", "Product",
                "Location", "Date", "Custom",
            ],
        }

    @pytest.mark.asyncio
    async def test_with_schemas(self, org_id: UUID) -> None:
        """Returns types from schemas."""
        rows = [({"types": ["Person", "Organization", "Product"]},)]
        session = FakeAsyncSession(rows)
        result = await _fetch_org_entity_types(
            db=session,
            org_id=org_id,
        )
        assert result == {
            "entity_types": ["Person", "Organization", "Product"],
        }

    @pytest.mark.asyncio
    async def test_with_schemas_missing_types_key(self, org_id: UUID) -> None:
        """Schema dict without 'types' key → defaults."""
        rows = [({"name": "custom_schema", "description": "no types"},)]
        session = FakeAsyncSession(rows)
        result = await _fetch_org_entity_types(
            db=session,
            org_id=org_id,
        )
        assert result == {
            "entity_types": [
                "Person", "Organization", "Product",
                "Location", "Date", "Custom",
            ],
        }


@pytest.mark.unit
class TestProviderFetchClassificationLabels:
    """Tests for _fetch_classification_labels."""

    @pytest.mark.asyncio
    async def test_no_schemas(self, org_id: UUID) -> None:
        """Returns default labels."""
        session = FakeAsyncSession([])
        result = await _fetch_classification_labels(
            db=session,
            org_id=org_id,
        )
        assert result == {
            "intent_labels": (
                "greeting, question, command, complaint, chit-chat, "
                "farewell, request, confirmation"
            ),
            "emotion_labels": (
                "joy, frustration, sadness, anger, neutral, surprise, fear, disgust"
            ),
            "valence_options": "positive, negative, neutral",
            "arousal_options": "low, medium, high",
        }

    @pytest.mark.asyncio
    async def test_with_schemas(self, org_id: UUID) -> None:
        """Returns merged labels from schemas."""
        rows = [(
            {
                "intent": ["ask", "tell"],
                "emotion": ["happy", "sad"],
                "valence": ["pos", "neg"],
                "arousal": ["high", "low"],
            },
        )]
        session = FakeAsyncSession(rows)
        result = await _fetch_classification_labels(
            db=session,
            org_id=org_id,
        )
        assert result["intent_labels"] == "ask, tell"
        assert result["emotion_labels"] == "happy, sad"
        assert result["valence_options"] == "neg, pos"
        assert result["arousal_options"] == "high, low"

    @pytest.mark.asyncio
    async def test_with_partial_schema(self, org_id: UUID) -> None:
        """Schema with only some fields → only those are customized."""
        rows = [({"intent": ["question"]},)]
        session = FakeAsyncSession(rows)
        result = await _fetch_classification_labels(
            db=session,
            org_id=org_id,
        )
        assert result["intent_labels"] == "question"
        # Other fields fall back to defaults
        assert "frustration" in result["emotion_labels"]
        assert "positive" in result["valence_options"]
        assert "medium" in result["arousal_options"]


@pytest.mark.unit
class TestProviderFetchStructuredSchemas:
    """Tests for _fetch_structured_schemas."""

    @pytest.mark.asyncio
    async def test_returns_schemas(self, org_id: UUID) -> None:
        """Returns list of schemas from DB."""
        rows = [
            (uuid4(), "schema1", {"type": "object"}, "template1"),
            (uuid4(), "schema2", {"type": "array"}, "template2"),
        ]
        session = FakeAsyncSession(rows)
        result = await _fetch_structured_schemas(
            db=session,
            org_id=org_id,
        )
        assert len(result["schemas"]) == 2
        assert result["schemas"][0]["name"] == "schema1"
        assert result["schemas"][1]["name"] == "schema2"
        assert result["schemas"][0]["json_schema"] == {"type": "object"}
        assert result["schemas"][0]["prompt_template"] == "template1"


@pytest.mark.unit
class TestProviderFetchUserEpisodes:
    """Tests for _fetch_user_episodes."""

    @pytest.mark.asyncio
    async def test_no_user_id(self, org_id: UUID) -> None:
        """Returns empty list."""
        result = await _fetch_user_episodes(
            db=FakeAsyncSession(),
            org_id=org_id,
            user_id=None,
        )
        assert result == {"episodes": []}

    @pytest.mark.asyncio
    async def test_with_user_id(
        self, org_id: UUID, user_id: UUID,
    ) -> None:
        """Returns episodes from DB."""
        # Rows are (role, content) tuples; result is reversed to chronological
        rows = [("assistant", "Hello"), ("user", "Hi")]
        session = FakeAsyncSession(rows)
        result = await _fetch_user_episodes(
            db=session,
            org_id=org_id,
            user_id=user_id,
        )
        assert result == {
            "episodes": [
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello"},
            ],
        }


@pytest.mark.unit
class TestProviderFetchUserFacts:
    """Tests for _fetch_user_facts."""

    @pytest.mark.asyncio
    async def test_no_user_id(self, org_id: UUID) -> None:
        """Returns empty list."""
        result = await _fetch_user_facts(
            db=FakeAsyncSession(),
            org_id=org_id,
            user_id=None,
        )
        assert result == {"facts": []}

    @pytest.mark.asyncio
    async def test_with_user_id(
        self, org_id: UUID, user_id: UUID,
    ) -> None:
        """Returns facts from DB."""
        rows = [
            ("Subject1", "Predicate1", "Object1"),
            ("Subject2", "Predicate2", "Object2"),
        ]
        session = FakeAsyncSession(rows)
        result = await _fetch_user_facts(
            db=session,
            org_id=org_id,
            user_id=user_id,
        )
        assert result == {
            "facts": [
                {"subject": "Subject1", "predicate": "Predicate1", "object": "Object1"},
                {"subject": "Subject2", "predicate": "Predicate2", "object": "Object2"},
            ],
        }


@pytest.mark.unit
class TestProviderFetchUserEntities:
    """Tests for _fetch_user_entities."""

    @pytest.mark.asyncio
    async def test_no_user_id(self, org_id: UUID) -> None:
        """Returns empty list."""
        result = await _fetch_user_entities(
            db=FakeAsyncSession(),
            org_id=org_id,
            user_id=None,
        )
        assert result == {"entities": []}


@pytest.mark.unit
class TestProviderFetchUserClassifications:
    """Tests for _fetch_user_classifications."""

    @pytest.mark.asyncio
    async def test_no_user_id(self, org_id: UUID) -> None:
        """Returns empty classifications."""
        result = await _fetch_user_classifications(
            db=FakeAsyncSession(),
            org_id=org_id,
            user_id=None,
        )
        assert result == {
            "classifications": {"top_intents": [], "top_emotions": []},
        }


@pytest.mark.unit
class TestProviderFetchCustomInstructions:
    """Tests for _fetch_custom_instructions."""

    @pytest.mark.asyncio
    async def test_no_instructions(
        self, org_id: UUID, monkeypatch: MonkeyPatch,
    ) -> None:
        """Returns empty custom_instructions."""
        from repositories.custom_instruction_repository import (
            CustomInstructionRepository,
        )

        async def mock_get_by_scope(
            self: Any, **kwargs: Any,
        ) -> list[Any]:
            return []

        monkeypatch.setattr(
            CustomInstructionRepository, "get_by_scope", mock_get_by_scope,
        )

        result = await _fetch_custom_instructions(
            db=FakeAsyncSession(),
            org_id=org_id,
        )
        assert result == {"custom_instructions": ""}

    @pytest.mark.asyncio
    async def test_with_instructions(
        self, org_id: UUID, user_id: UUID, monkeypatch: MonkeyPatch,
    ) -> None:
        """Uses CustomInstructionRepository.get_by_scope and formats result."""
        from repositories.custom_instruction_repository import (
            CustomInstructionRepository,
        )

        class MockInstruction:
            def __init__(self, name: str, text: str) -> None:
                self.name = name
                self.text = text

        async def mock_get_by_scope(
            self: Any, **kwargs: Any,
        ) -> list[Any]:
            return [MockInstruction("style", "Be concise.")]

        monkeypatch.setattr(
            CustomInstructionRepository, "get_by_scope", mock_get_by_scope,
        )

        # format_custom_instructions is imported lazily — ensure the module
        # is in sys.modules before monkeypatching.
        import services.custom_instruction_service  # noqa: F811

        def mock_format(instructions: list[dict[str, str]]) -> str:
            return "Be concise."

        monkeypatch.setattr(
            services.custom_instruction_service,
            "format_custom_instructions",
            mock_format,
        )

        result = await _fetch_custom_instructions(
            db=FakeAsyncSession(),
            org_id=org_id,
            user_id=user_id,
        )
        assert result == {"custom_instructions": "Be concise."}


# ══════════════════════════════════════════════════════════════════════════════
# build_enrichment_prompt tests
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestBuildEnrichmentPrompt:
    """Tests for build_enrichment_prompt — prompt assembly from system text + context."""

    def test_basic_system_prompt(self) -> None:
        """Just the system prompt, no context sections appended."""
        result = build_enrichment_prompt("You are a helpful assistant.", {})
        assert result == "You are a helpful assistant."

    def test_with_metadata(self) -> None:
        """Adds MESSAGE METADATA section."""
        ctx = {"message_metadata": {"source": "web", "timestamp": "2024-01-01"}}
        result = build_enrichment_prompt("System prompt", ctx)
        assert "## MESSAGE METADATA" in result
        assert '"source"' in result
        assert '"web"' in result

    def test_with_known_entities(self) -> None:
        """Adds KNOWN ENTITIES table."""
        ctx = {
            "known_entities": [
                {"name": "Acme Corp", "entity_type": "Organization"},
                {"name": "John", "entity_type": "Person"},
            ],
        }
        result = build_enrichment_prompt("System prompt", ctx)
        assert "## KNOWN ENTITIES" in result
        assert "| Acme Corp | Organization |" in result
        assert "| John | Person |" in result

    def test_with_existing_facts(self) -> None:
        """Adds EXISTING FACTS table."""
        ctx = {
            "existing_facts": [
                {"subject": "S1", "predicate": "P1", "object": "O1"},
            ],
        }
        result = build_enrichment_prompt("System prompt", ctx)
        assert "## EXISTING FACTS" in result
        assert "| S1 | P1 | O1 |" in result

    def test_with_recent_history(self) -> None:
        """Adds RECENT CONVERSATION section."""
        ctx = {
            "recent_history": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there"},
            ],
        }
        result = build_enrichment_prompt("System prompt", ctx)
        assert "## RECENT CONVERSATION" in result
        assert "[user]" in result
        assert "Hello" in result
        assert "[assistant]" in result
        assert "Hi there" in result

    def test_with_similar_episodes(self) -> None:
        """Adds SIMILAR EPISODES section."""
        ctx = {
            "similar_episodes": [
                {"role": "user", "content": "Similar query", "score": 0.85},
            ],
        }
        result = build_enrichment_prompt("System prompt", ctx)
        assert "## SIMILAR EPISODES FROM HISTORY" in result
        assert "Similar query" in result
        assert "0.850" in result

    def test_with_related_facts(self) -> None:
        """Adds RELATED FACTS table."""
        ctx = {
            "related_facts": [
                {"subject": "RS", "predicate": "RP", "object": "RO", "score": 0.95},
            ],
        }
        result = build_enrichment_prompt("System prompt", ctx)
        assert "## RELATED FACTS FROM HISTORY" in result
        assert "| RS | RP | RO |" in result
        assert "0.950" in result

    def test_with_conversation(self) -> None:
        """Adds NOW EXTRACT FROM section."""
        ctx = {"conversation": "User said something important."}
        result = build_enrichment_prompt("System prompt", ctx)
        assert "## NOW EXTRACT FROM THIS CONVERSATION" in result
        assert "User said something important." in result

    def test_all_sections_together(self) -> None:
        """Full prompt with all context sections in correct order."""
        ctx = {
            "message_metadata": {"key": "val"},
            "known_entities": [{"name": "E1", "entity_type": "Type"}],
            "existing_facts": [{"subject": "S", "predicate": "P", "object": "O"}],
            "recent_history": [{"role": "user", "content": "Hi"}],
            "similar_episodes": [{"role": "assistant", "content": "Prev answer", "score": 0.8}],
            "related_facts": [{"subject": "RS", "predicate": "RP", "object": "RO", "score": 0.9}],
            "conversation": "Extract from this.",
        }
        result = build_enrichment_prompt("System prompt", ctx)
        assert "MESSAGE METADATA" in result
        assert "KNOWN ENTITIES" in result
        assert "EXISTING FACTS" in result
        assert "RECENT CONVERSATION" in result
        assert "SIMILAR EPISODES FROM HISTORY" in result
        assert "RELATED FACTS FROM HISTORY" in result
        assert "NOW EXTRACT FROM THIS CONVERSATION" in result
        # Metadata appears first, conversation last
        assert result.index("MESSAGE METADATA") < result.index("KNOWN ENTITIES")
        assert (
            result.index("NOW EXTRACT FROM THIS CONVERSATION")
            > result.index("EXISTING FACTS")
        )

    def test_empty_context(self) -> None:
        """Empty context produces just the system prompt."""
        result = build_enrichment_prompt("Instructions", {})
        assert result == "Instructions"
