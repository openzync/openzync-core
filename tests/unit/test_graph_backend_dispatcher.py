"""Unit tests for GraphBackendDispatcher — registry + resolution logic.

These tests verify:
- Backend registration and overwrite behaviour.
- ``resolve_and_create`` with valid, disabled, and missing configs.
- ``resolve_backend_name`` pure-lookup path.
- Error handling for unknown backends.
- Backend-specific kwargs extraction (``max_traversal_depth``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, sentinel
from uuid import UUID

import pytest

from core.graph_backend import GraphBackendDispatcher
from packages.graph_backend.surrealdb import SurrealGraphBackend


@pytest.mark.unit
class TestGraphBackendDispatcher:
    """Tests for the dispatcher's registry + resolution logic."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")

    # ── Fixtures ──────────────────────────────────────────────────────────────────

    @pytest.fixture
    def dispatcher(self) -> GraphBackendDispatcher:
        """A clean dispatcher with one mock backend registered."""
        disp = GraphBackendDispatcher()
        mock_cls = MagicMock()
        mock_instance = AsyncMock()
        mock_cls.return_value = mock_instance
        disp.register("test_backend", mock_cls)
        return disp

    @pytest.fixture
    def mock_db(self) -> MagicMock:
        """A mock AsyncSession."""
        return MagicMock()

    @pytest.fixture
    def org_config_postgres(self) -> MagicMock:
        """Org config with postgres backend and traversal depth."""
        cfg = MagicMock()
        cfg.graph_backend = "postgres"
        cfg.graph_max_traversal_depth = 3
        return cfg

    @pytest.fixture
    def org_config_disabled(self) -> MagicMock:
        """Org config with graph disabled."""
        cfg = MagicMock()
        cfg.graph_backend = "none"
        return cfg

    # ── Registration ─────────────────────────────────────────────────────────────

    def test_register_backend(self, dispatcher: GraphBackendDispatcher) -> None:
        """A registered backend can be resolved and created."""
        name = dispatcher.resolve_backend_name(
            MagicMock(graph_backend="test_backend")
        )
        assert name == "test_backend"

    def test_register_overwrite(self) -> None:
        """Re-registering a name replaces the previous class."""
        disp = GraphBackendDispatcher()
        old_cls = MagicMock()
        new_cls = MagicMock()
        disp.register("same_name", old_cls)
        disp.register("same_name", new_cls)

        cfg = MagicMock(graph_backend="same_name")
        backend = disp.resolve_and_create(cfg, MagicMock())

        old_cls.assert_not_called()
        new_cls.assert_called_once()
        assert backend is new_cls.return_value

    # ── resolve_backend_name (pure lookup) ──────────────────────────────────────

    def test_resolve_name_returns_none_when_config_is_none(
        self, dispatcher: GraphBackendDispatcher
    ) -> None:
        """None org config → graph disabled."""
        assert dispatcher.resolve_backend_name(None) is None

    def test_resolve_name_returns_none_when_backend_is_none(
        self, dispatcher: GraphBackendDispatcher
    ) -> None:
        """Unset graph_backend → graph disabled."""
        cfg = MagicMock(graph_backend=None)
        assert dispatcher.resolve_backend_name(cfg) is None

    def test_resolve_name_returns_none_when_disabled(
        self, dispatcher: GraphBackendDispatcher, org_config_disabled: MagicMock
    ) -> None:
        """graph_backend='none' → graph disabled."""
        assert dispatcher.resolve_backend_name(org_config_disabled) is None

    def test_resolve_name_returns_name(
        self, dispatcher: GraphBackendDispatcher
    ) -> None:
        """Valid graph_backend name → returned as-is."""
        cfg = MagicMock(graph_backend="test_backend")
        assert dispatcher.resolve_backend_name(cfg) == "test_backend"

    # ── resolve_and_create ──────────────────────────────────────────────────────

    def test_create_with_valid_config(
        self, dispatcher: GraphBackendDispatcher, mock_db: MagicMock
    ) -> None:
        """Valid config → backend instance created and returned."""
        cfg = MagicMock(graph_backend="test_backend")
        backend = dispatcher.resolve_and_create(cfg, mock_db)

        assert backend is not None
        # The mock class stored in _registry["test_backend"] was called with db=
        # We can check this via the class's return_value being returned

    def test_create_returns_none_when_config_is_none(
        self, dispatcher: GraphBackendDispatcher, mock_db: MagicMock
    ) -> None:
        """None org config → returns None."""
        assert dispatcher.resolve_and_create(None, mock_db) is None

    def test_create_returns_none_when_disabled(
        self,
        dispatcher: GraphBackendDispatcher,
        org_config_disabled: MagicMock,
        mock_db: MagicMock,
    ) -> None:
        """graph_backend='none' → returns None."""
        assert dispatcher.resolve_and_create(org_config_disabled, mock_db) is None

    def test_create_raises_for_unknown_backend(
        self, dispatcher: GraphBackendDispatcher, mock_db: MagicMock
    ) -> None:
        """Unknown backend name → raises ValueError."""
        cfg = MagicMock(graph_backend="does_not_exist")
        with pytest.raises(ValueError, match="Unknown graph backend.*does_not_exist"):
            dispatcher.resolve_and_create(cfg, mock_db)

    def test_create_passes_db_to_backend_constructor(
        self, mock_db: MagicMock
    ) -> None:
        """The postgres backend class receives the db argument."""
        disp = GraphBackendDispatcher()
        mock_cls = MagicMock()
        disp.register("postgres", mock_cls)

        cfg = MagicMock(graph_backend="postgres")
        cfg.graph_max_traversal_depth = None  # prevent MagicMock default
        disp.resolve_and_create(cfg, mock_db)

        mock_cls.assert_called_once_with(db=mock_db)

    # ── Postgres-specific kwargs ────────────────────────────────────────────────

    def test_postgres_receives_max_traversal_depth(
        self, mock_db: MagicMock
    ) -> None:
        """Postgres backend gets max_traversal_depth from org_config."""
        disp = GraphBackendDispatcher()
        mock_cls = MagicMock()
        disp.register("postgres", mock_cls)

        cfg = MagicMock(graph_backend="postgres")
        cfg.graph_max_traversal_depth = 5
        disp.resolve_and_create(cfg, mock_db)

        mock_cls.assert_called_once_with(db=mock_db, max_traversal_depth=5)

    def test_postgres_without_max_traversal_depth(
        self, mock_db: MagicMock
    ) -> None:
        """When graph_max_traversal_depth is None, it's not passed."""
        disp = GraphBackendDispatcher()
        mock_cls = MagicMock()
        disp.register("postgres", mock_cls)

        cfg = MagicMock(graph_backend="postgres")
        cfg.graph_max_traversal_depth = None
        disp.resolve_and_create(cfg, mock_db)

        mock_cls.assert_called_once_with(db=mock_db)

    def test_non_postgres_backend_ignores_extra_kwargs(
        self, mock_db: MagicMock
    ) -> None:
        """Non-postgres backends don't receive postgres-specific kwargs.

        Since ``db`` is now also postgres-specific, a non-postgres
        backend receives no positional arguments (unless it has
        backend-specific kwargs like ``surreal``).
        """
        disp = GraphBackendDispatcher()
        mock_cls = MagicMock()
        disp.register("custom", mock_cls)

        cfg = MagicMock(graph_backend="custom")
        cfg.graph_max_traversal_depth = 5  # should be ignored
        disp.resolve_and_create(cfg, mock_db)

        mock_cls.assert_called_once_with()  # no args — db is postgres-only now

    # ── create_all_backends ─────────────────────────────────────────────────────

    def test_create_all_backends_empty(self, mock_db: MagicMock) -> None:
        """Empty registry → empty list."""
        disp = GraphBackendDispatcher()
        backends = disp.create_all_backends(mock_db)
        assert backends == []

    def test_create_all_backends_single(
        self, dispatcher: GraphBackendDispatcher, mock_db: MagicMock
    ) -> None:
        """One registered backend → list with one instance."""
        backends = dispatcher.create_all_backends(mock_db)
        assert len(backends) == 1

    def test_create_all_backends_multiple(self, mock_db: MagicMock) -> None:
        """Multiple registered backends → each gets backend-specific kwargs."""
        disp = GraphBackendDispatcher()
        cls_a = MagicMock()
        cls_b = MagicMock()
        disp.register("postgres", cls_a)   # postgres receives db
        disp.register("other", cls_b)      # non-postgres receives no db

        backends = disp.create_all_backends(mock_db)

        assert len(backends) == 2
        cls_a.assert_called_once_with(db=mock_db)
        cls_b.assert_called_once_with()  # no db — only surreal backends get surreal

    def test_create_all_backends_passes_depth_to_postgres(
        self, mock_db: MagicMock
    ) -> None:
        """Postgres backend receives max_traversal_depth from org_config."""
        disp = GraphBackendDispatcher()
        mock_cls = MagicMock()
        disp.register("postgres", mock_cls)

        cfg = MagicMock(graph_backend="postgres")
        cfg.graph_max_traversal_depth = 4
        backends = disp.create_all_backends(mock_db, cfg)

        assert len(backends) == 1
        mock_cls.assert_called_once_with(db=mock_db, max_traversal_depth=4)

    def test_create_all_backends_without_org_config(
        self, mock_db: MagicMock
    ) -> None:
        """No org_config → backends created without extra kwargs."""
        disp = GraphBackendDispatcher()
        mock_cls = MagicMock()
        disp.register("postgres", mock_cls)

        backends = disp.create_all_backends(mock_db)
        assert len(backends) == 1
        mock_cls.assert_called_once_with(db=mock_db)

    def test_create_all_backends_returns_all_registered(
        self, mock_db: MagicMock
    ) -> None:
        """create_all_backends includes every registered name."""
        disp = GraphBackendDispatcher()
        disp.register("alpha", MagicMock())
        disp.register("beta", MagicMock())

        backends = disp.create_all_backends(mock_db)
        assert len(backends) == 2

    # ── init_dispatcher factory ─────────────────────────────────────────────────

    def test_init_dispatcher(self) -> None:
        """init_dispatcher() returns a populated dispatcher with postgres."""
        from core.graph_backend import init_dispatcher

        disp = init_dispatcher()

        # Postgres is registered
        assert disp.resolve_backend_name(MagicMock(graph_backend="postgres")) == "postgres"
        # Unknown backends still fail
        assert disp.resolve_backend_name(MagicMock(graph_backend="unknown")) == "unknown"

    def test_init_dispatcher_creates_postgres_backend(self) -> None:
        """resolve_and_create with 'postgres' creates a PostgresGraphBackend."""
        from core.graph_backend import init_dispatcher
        from packages.graph_backend.postgres import PostgresGraphBackend

        disp = init_dispatcher()
        cfg = MagicMock(graph_backend="postgres")
        cfg.graph_max_traversal_depth = 2
        mock_db = MagicMock()

        backend = disp.resolve_and_create(cfg, mock_db)

        assert isinstance(backend, PostgresGraphBackend)

    # ── SurrealDB-specific ──────────────────────────────────────────────────────

    def test_resolve_and_create_surrealdb(self, mock_db: MagicMock) -> None:
        """resolve_and_create with 'surrealdb' creates a SurrealGraphBackend."""
        disp = GraphBackendDispatcher()
        disp.register("surrealdb", SurrealGraphBackend)

        mock_surreal = AsyncMock()
        cfg = MagicMock(graph_backend="surrealdb")
        cfg.graph_max_traversal_depth = 3
        backend = disp.resolve_and_create(cfg, mock_db, surreal=mock_surreal)

        assert isinstance(backend, SurrealGraphBackend)
        assert backend._max_depth == 3
        assert backend._surreal is mock_surreal

    def test_create_all_backends_includes_surrealdb(
        self, mock_db: MagicMock
    ) -> None:
        """create_all_backends includes SurrealGraphBackend when registered."""
        disp = GraphBackendDispatcher()
        disp.register("surrealdb", SurrealGraphBackend)

        mock_surreal = AsyncMock()
        backends = disp.create_all_backends(mock_db, surreal=mock_surreal)

        assert any(isinstance(b, SurrealGraphBackend) for b in backends)
        surreal_bk = next(b for b in backends if isinstance(b, SurrealGraphBackend))
        assert surreal_bk._surreal is mock_surreal

    def test_surreal_kwarg_not_passed_to_postgres(
        self, mock_db: MagicMock
    ) -> None:
        """surreal kwarg is only passed to SurrealGraphBackend, not Postgres.

        Postgres receives ``db`` but not ``surreal``.
        SurrealDB receives ``surreal`` but not ``db``.
        """
        disp = GraphBackendDispatcher()
        mock_postgres_cls = MagicMock()
        disp.register("postgres", mock_postgres_cls)
        disp.register("surrealdb", SurrealGraphBackend)

        mock_surreal = AsyncMock()
        backends = disp.create_all_backends(mock_db, surreal=mock_surreal)

        assert len(backends) == 2
        # Postgres constructor only got db= — no surreal keyword
        mock_postgres_cls.assert_called_once_with(db=mock_db)
