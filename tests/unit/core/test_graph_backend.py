"""Unit tests for GraphBackendDispatcher — registry, resolution, and factory.

This file tests the *dispatcher/factory layer* in ``core/graph_backend.py``
(``GraphBackendDispatcher`` and ``init_dispatcher``).  The existing
``tests/unit/test_graph_backend_dispatcher.py`` covers the core registry and
resolution logic; this file fills gaps for:

- ``init_dispatcher`` registering all 3 backends (postgres, surrealdb, falkordb)
- FalkorDB-specific kwargs and resolution
- ``surreal=None`` / ``falkordb_client=None`` skip behaviour in
  ``create_all_backends``
- Mixed backend scenarios with all 3 backends

Backend **implementations** are tested in
``tests/unit/test_graph_backend_contract.py`` — do not duplicate there.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, sentinel
from uuid import UUID

import pytest


@pytest.mark.unit
class TestInitDispatcher:
    """init_dispatcher — factory that creates and populates a dispatcher.

    The existing test (``test_graph_backend_dispatcher.py::test_init_dispatcher``)
    only checks that ``"postgres"`` is registered.  We verify all 3 backends.
    """

    def test_init_dispatcher_registers_postgres(self) -> None:
        """Postgres backend is registered."""
        from core.graph_backend import init_dispatcher

        disp = init_dispatcher()
        assert disp.resolve_backend_name(MagicMock(graph_backend="postgres")) == "postgres"

    def test_init_dispatcher_registers_surrealdb(self) -> None:
        """SurrealDB backend is registered."""
        from core.graph_backend import init_dispatcher

        disp = init_dispatcher()
        assert disp.resolve_backend_name(MagicMock(graph_backend="surrealdb")) == "surrealdb"

    def test_init_dispatcher_registers_falkordb(self) -> None:
        """FalkorDB backend is registered."""
        from core.graph_backend import init_dispatcher

        disp = init_dispatcher()
        assert disp.resolve_backend_name(MagicMock(graph_backend="falkordb")) == "falkordb"

    def test_init_dispatcher_knows_all_three(self) -> None:
        """All three backend names are present in the registry."""
        from core.graph_backend import init_dispatcher

        disp = init_dispatcher()
        assert "postgres" in disp._registry
        assert "surrealdb" in disp._registry
        assert "falkordb" in disp._registry

    def test_init_dispatcher_creates_postgres_instance(self) -> None:
        """resolve_and_create with 'postgres' returns a PostgresGraphBackend."""
        from core.graph_backend import init_dispatcher
        from packages.graph_backend.postgres import PostgresGraphBackend

        disp = init_dispatcher()
        mock_db = MagicMock()
        cfg = MagicMock(graph_backend="postgres", graph_max_traversal_depth=None)
        backend = disp.resolve_and_create(cfg, mock_db)
        assert isinstance(backend, PostgresGraphBackend)

    def test_init_dispatcher_creates_surrealdb_instance(self) -> None:
        """resolve_and_create with 'surrealdb' returns a SurrealGraphBackend."""
        from core.graph_backend import init_dispatcher
        from packages.graph_backend.surrealdb import SurrealGraphBackend

        disp = init_dispatcher()
        mock_surreal = AsyncMock()
        cfg = MagicMock(graph_backend="surrealdb", graph_max_traversal_depth=None)
        backend = disp.resolve_and_create(cfg, MagicMock(), surreal=mock_surreal)
        assert isinstance(backend, SurrealGraphBackend)

    def test_init_dispatcher_creates_falkordb_instance(self) -> None:
        """resolve_and_create with 'falkordb' returns a FalkorGraphBackend."""
        from core.graph_backend import init_dispatcher
        from packages.graph_backend.falkordb import FalkorGraphBackend

        disp = init_dispatcher()
        mock_client = MagicMock()
        cfg = MagicMock(graph_backend="falkordb", graph_max_traversal_depth=None)
        backend = disp.resolve_and_create(cfg, MagicMock(), falkordb_client=mock_client)
        assert isinstance(backend, FalkorGraphBackend)


@pytest.mark.unit
class TestFalkorDBResolution:
    """FalkorDB-specific kwargs resolution — the gap in existing tests."""

    def test_falkordb_receives_client(self) -> None:
        """FalkorDB backend receives client from falkordb_client kwarg."""
        from core.graph_backend import GraphBackendDispatcher

        disp = GraphBackendDispatcher()
        mock_cls = MagicMock()
        disp.register("falkordb", mock_cls)

        mock_client = MagicMock()
        cfg = MagicMock(graph_backend="falkordb", graph_max_traversal_depth=None)
        disp.resolve_and_create(cfg, MagicMock(), falkordb_client=mock_client)

        mock_cls.assert_called_once_with(client=mock_client)

    def test_falkordb_receives_max_traversal_depth(self) -> None:
        """FalkorDB backend receives max_traversal_depth from org_config."""
        from core.graph_backend import GraphBackendDispatcher

        disp = GraphBackendDispatcher()
        mock_cls = MagicMock()
        disp.register("falkordb", mock_cls)

        mock_client = MagicMock()
        cfg = MagicMock(graph_backend="falkordb", graph_max_traversal_depth=4)
        disp.resolve_and_create(cfg, MagicMock(), falkordb_client=mock_client)

        mock_cls.assert_called_once_with(client=mock_client, max_traversal_depth=4)

    def test_falkordb_without_client(self) -> None:
        """When falkordb_client is None, constructor still works (handles internally)."""
        from core.graph_backend import GraphBackendDispatcher

        disp = GraphBackendDispatcher()
        mock_cls = MagicMock()
        disp.register("falkordb", mock_cls)

        cfg = MagicMock(graph_backend="falkordb", graph_max_traversal_depth=None)
        # falkordb_client=None — client is not passed to constructor
        disp.resolve_and_create(cfg, MagicMock(), falkordb_client=None)

        mock_cls.assert_called_once_with()


@pytest.mark.unit
class TestCreateAllBackendsSkipping:
    """create_all_backends skips backends whose client is None.

    The existing tests cover the happy path.  These test the skip paths:
    - SurrealDB is skipped when ``surreal=None``
    - FalkorDB is skipped when ``falkordb_client=None``
    - Postgres is never skipped (always created)
    - Mixed: all three created when all clients present
    """

    def test_skips_surrealdb_when_surreal_is_none(self) -> None:
        """SurrealDB backend is skipped when surreal is None."""
        from core.graph_backend import GraphBackendDispatcher

        disp = GraphBackendDispatcher()
        pg_cls = MagicMock()
        sd_cls = MagicMock()
        disp.register("postgres", pg_cls)
        disp.register("surrealdb", sd_cls)

        mock_db = MagicMock()
        backends = disp.create_all_backends(mock_db, surreal=None)

        assert len(backends) == 1  # only postgres
        pg_cls.assert_called_once_with(db=mock_db)
        sd_cls.assert_not_called()

    def test_skips_falkordb_when_client_is_none(self) -> None:
        """FalkorDB backend is skipped when falkordb_client is None."""
        from core.graph_backend import GraphBackendDispatcher

        disp = GraphBackendDispatcher()
        pg_cls = MagicMock()
        fd_cls = MagicMock()
        disp.register("postgres", pg_cls)
        disp.register("falkordb", fd_cls)

        mock_db = MagicMock()
        backends = disp.create_all_backends(mock_db, falkordb_client=None)

        assert len(backends) == 1  # only postgres
        pg_cls.assert_called_once_with(db=mock_db)
        fd_cls.assert_not_called()

    def test_creates_postgres_when_surreal_and_falkor_are_none(self) -> None:
        """Postgres is always created, even when other clients are None."""
        from core.graph_backend import GraphBackendDispatcher

        disp = GraphBackendDispatcher()
        pg_cls = MagicMock()
        sd_cls = MagicMock()
        fd_cls = MagicMock()
        disp.register("postgres", pg_cls)
        disp.register("surrealdb", sd_cls)
        disp.register("falkordb", fd_cls)

        mock_db = MagicMock()
        backends = disp.create_all_backends(mock_db, surreal=None, falkordb_client=None)

        assert len(backends) == 1
        pg_cls.assert_called_once_with(db=mock_db)

    def test_creates_all_three_when_clients_provided(self) -> None:
        """All three backends created when surreal and falkordb_client given."""
        from core.graph_backend import GraphBackendDispatcher

        disp = GraphBackendDispatcher()
        pg_cls = MagicMock()
        sd_cls = MagicMock()
        fd_cls = MagicMock()
        disp.register("postgres", pg_cls)
        disp.register("surrealdb", sd_cls)
        disp.register("falkordb", fd_cls)

        mock_db = MagicMock()
        mock_surreal = AsyncMock()
        mock_client = MagicMock()

        backends = disp.create_all_backends(
            mock_db,
            surreal=mock_surreal,
            falkordb_client=mock_client,
        )

        assert len(backends) == 3
        pg_cls.assert_called_once_with(db=mock_db)
        sd_cls.assert_called_once_with(surreal=mock_surreal)
        fd_cls.assert_called_once_with(client=mock_client)

    def test_skips_surrealdb_only(self) -> None:
        """Only SurrealDB is skipped; Postgres and FalkorDB are created."""
        from core.graph_backend import GraphBackendDispatcher

        disp = GraphBackendDispatcher()
        pg_cls = MagicMock()
        sd_cls = MagicMock()
        fd_cls = MagicMock()
        disp.register("postgres", pg_cls)
        disp.register("surrealdb", sd_cls)
        disp.register("falkordb", fd_cls)

        mock_db = MagicMock()
        mock_client = MagicMock()
        backends = disp.create_all_backends(mock_db, surreal=None, falkordb_client=mock_client)

        assert len(backends) == 2
        pg_cls.assert_called_once_with(db=mock_db)
        fd_cls.assert_called_once_with(client=mock_client)
        sd_cls.assert_not_called()


@pytest.mark.unit
class TestDispatcherErrorHandling:
    """Edge cases and error handling not covered by existing dispatcher tests."""

    def test_unknown_backend_in_resolve_and_create(self) -> None:
        """An unknown backend name raises ValueError with a helpful message."""
        from core.graph_backend import GraphBackendDispatcher

        disp = GraphBackendDispatcher()
        cfg = MagicMock(graph_backend="nonexistent")
        with pytest.raises(ValueError, match="Unknown graph backend.*nonexistent"):
            disp.resolve_and_create(cfg, MagicMock())

    def test_unknown_backend_in_create_all_still_created(self) -> None:
        """create_all_backends creates all registered backends even unknown to code."""
        from core.graph_backend import GraphBackendDispatcher

        disp = GraphBackendDispatcher()
        cls_a = MagicMock()
        disp.register("unknown_but_registered", cls_a)

        backends = disp.create_all_backends(MagicMock())
        assert len(backends) == 1

    def test_resolve_and_create_passes_depth_when_available(self) -> None:
        """max_traversal_depth is passed when set in org_config for falkordb."""
        from core.graph_backend import GraphBackendDispatcher

        disp = GraphBackendDispatcher()
        mock_cls = MagicMock()
        disp.register("falkordb", mock_cls)

        cfg = MagicMock(graph_backend="falkordb", graph_max_traversal_depth=3)
        mock_client = MagicMock()
        disp.resolve_and_create(cfg, MagicMock(), falkordb_client=mock_client)

        mock_cls.assert_called_once_with(client=mock_client, max_traversal_depth=3)

    def test_resolve_and_create_no_depth_when_none(self) -> None:
        """max_traversal_depth is omitted when org_config has it as None."""
        from core.graph_backend import GraphBackendDispatcher

        disp = GraphBackendDispatcher()
        mock_cls = MagicMock()
        disp.register("falkordb", mock_cls)

        cfg = MagicMock(graph_backend="falkordb", graph_max_traversal_depth=None)
        mock_client = MagicMock()
        disp.resolve_and_create(cfg, MagicMock(), falkordb_client=mock_client)

        mock_cls.assert_called_once_with(client=mock_client)


@pytest.mark.unit
class TestBackendSpecificKwargsIsolation:
    """Each backend receives only its own kwargs — no cross-contamination.

    Postgres gets ``db``.
    SurrealDB gets ``surreal``.
    FalkorDB gets ``client``.
    """

    def test_postgres_does_not_get_surreal_or_client(self) -> None:
        """Postgres only receives db, not surreal or client."""
        from core.graph_backend import GraphBackendDispatcher

        disp = GraphBackendDispatcher()
        pg_cls = MagicMock()
        disp.register("postgres", pg_cls)

        mock_db = MagicMock()
        mock_surreal = AsyncMock()
        mock_client = MagicMock()
        cfg = MagicMock(graph_backend="postgres", graph_max_traversal_depth=2)

        disp.resolve_and_create(cfg, mock_db, surreal=mock_surreal, falkordb_client=mock_client)
        pg_cls.assert_called_once_with(db=mock_db, max_traversal_depth=2)

    def test_surrealdb_does_not_get_db_or_client(self) -> None:
        """SurrealDB only receives surreal, not db or client."""
        from core.graph_backend import GraphBackendDispatcher

        disp = GraphBackendDispatcher()
        sd_cls = MagicMock()
        disp.register("surrealdb", sd_cls)

        mock_surreal = AsyncMock()
        mock_client = MagicMock()
        cfg = MagicMock(graph_backend="surrealdb", graph_max_traversal_depth=3)

        disp.resolve_and_create(cfg, MagicMock(), surreal=mock_surreal, falkordb_client=mock_client)
        sd_cls.assert_called_once_with(surreal=mock_surreal, max_traversal_depth=3)

    def test_falkordb_does_not_get_db_or_surreal(self) -> None:
        """FalkorDB only receives client, not db or surreal."""
        from core.graph_backend import GraphBackendDispatcher

        disp = GraphBackendDispatcher()
        fd_cls = MagicMock()
        disp.register("falkordb", fd_cls)

        mock_surreal = AsyncMock()
        mock_client = MagicMock()
        cfg = MagicMock(graph_backend="falkordb", graph_max_traversal_depth=4)

        disp.resolve_and_create(cfg, MagicMock(), surreal=mock_surreal, falkordb_client=mock_client)
        fd_cls.assert_called_once_with(client=mock_client, max_traversal_depth=4)
