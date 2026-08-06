"""Unit tests for ``workers.backend`` — graph backend resolution inside ARQ workers.

Tests cover:

- ``resolve_graph_backend()`` — all resolution paths: raise on failure
  (missing dispatcher, dispatcher error, None result for a configured
  backend), None on explicitly-disabled graph (no org config, ``"none"``).
- ``_resolve_org_config()`` — primary path (with/without ``bao_client``),
  ImportError/Exception fallthrough, DB fallback, and full failure.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from core.exceptions import GraphBackendUnavailableError


# ═══════════════════════════════════════════════════════════════════════════════
# resolve_graph_backend
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestResolveGraphBackend:
    """Decision-branch coverage for ``resolve_graph_backend``.

    Contract: ``None`` is returned only when graph is explicitly disabled
    (no org config, or backend name ``"none"``/empty).  A configured
    backend that cannot be resolved — missing dispatcher, dispatcher
    failure, or a ``None`` resolution result — raises
    ``GraphBackendUnavailableError``.  There is no Postgres fallback.
    """

    ORG_ID: UUID = uuid4()

    @pytest.fixture
    def mock_db(self) -> MagicMock:
        """A mock ``AsyncSession`` — never called directly in these tests."""
        return MagicMock()

    @pytest.fixture
    def mock_org_config(self) -> MagicMock:
        """A per-org config stub with ``graph_backend`` set."""
        cfg = MagicMock()
        cfg.graph_backend = "postgres"
        return cfg

    @pytest.fixture
    def mock_dispatcher(self) -> MagicMock:
        """A mock ``GraphBackendDispatcher`` that resolves successfully."""
        d = MagicMock()
        d.resolve_and_create.return_value = MagicMock()
        return d

    @pytest.fixture
    def mock_surreal_pool(self) -> AsyncMock:
        """A mock surreal connection pool returning a connection."""
        pool = AsyncMock()
        pool.get_or_create = AsyncMock(return_value=MagicMock())
        return pool

    # ── No dispatcher ─────────────────────────────────────────────────────

    async def test_no_dispatcher_raises(
        self,
        mock_db: MagicMock,
    ) -> None:
        """No ``graph_backend_dispatcher`` in ctx → raises (worker misconfig)."""
        from workers.backend import resolve_graph_backend

        ctx: dict = {}
        with pytest.raises(
            GraphBackendUnavailableError, match="graph_backend_dispatcher",
        ):
            await resolve_graph_backend(ctx, self.ORG_ID, mock_db)

    # ── Org config is None ────────────────────────────────────────────────

    async def test_org_config_none_returns_none(
        self,
        mock_db: MagicMock,
        mock_dispatcher: MagicMock,
    ) -> None:
        """Org config resolves to ``None`` → graph disabled, returns ``None``."""
        with patch("workers.backend._resolve_org_config", AsyncMock(return_value=None)):
            from workers.backend import resolve_graph_backend

            ctx = {"graph_backend_dispatcher": mock_dispatcher}
            result = await resolve_graph_backend(ctx, self.ORG_ID, mock_db)

        assert result is None

    # ── Backend name is empty / "none" / None ─────────────────────────────

    @pytest.mark.parametrize("backend_name", [None, "", "none"])
    async def test_disabled_backend_returns_none(
        self,
        backend_name: str | None,
        mock_db: MagicMock,
        mock_dispatcher: MagicMock,
    ) -> None:
        """Backend name is ``None`` / ``""`` / ``"none"`` → returns ``None``."""
        cfg = MagicMock()
        cfg.graph_backend = backend_name

        with patch("workers.backend._resolve_org_config", AsyncMock(return_value=cfg)):
            from workers.backend import resolve_graph_backend

            ctx = {"graph_backend_dispatcher": mock_dispatcher}
            result = await resolve_graph_backend(ctx, self.ORG_ID, mock_db)

        assert result is None

    # ── Successful resolution ─────────────────────────────────────────────

    async def test_backend_resolved_successfully(
        self,
        mock_db: MagicMock,
        mock_org_config: MagicMock,
        mock_dispatcher: MagicMock,
    ) -> None:
        """Dispatcher resolves a backend → returns it directly."""
        with patch("workers.backend._resolve_org_config", AsyncMock(return_value=mock_org_config)):
            from workers.backend import resolve_graph_backend

            ctx = {"graph_backend_dispatcher": mock_dispatcher}
            result = await resolve_graph_backend(ctx, self.ORG_ID, mock_db)

        mock_dispatcher.resolve_and_create.assert_called_once_with(
            org_config=mock_org_config,
            db=mock_db,
            surreal=None,
            falkordb_client=None,
        )
        assert result is mock_dispatcher.resolve_and_create.return_value

    async def test_surrealdb_backend_with_pool(
        self,
        mock_db: MagicMock,
        mock_dispatcher: MagicMock,
        mock_surreal_pool: AsyncMock,
    ) -> None:
        """SurrealDB backend + pool in ctx → acquires connection, resolves."""
        cfg = MagicMock()
        cfg.graph_backend = "surrealdb"

        with patch("workers.backend._resolve_org_config", AsyncMock(return_value=cfg)):
            from workers.backend import resolve_graph_backend

            ctx = {
                "graph_backend_dispatcher": mock_dispatcher,
                "surreal_connection_pool": mock_surreal_pool,
            }
            result = await resolve_graph_backend(ctx, self.ORG_ID, mock_db)

        mock_surreal_pool.get_or_create.assert_awaited_once_with(self.ORG_ID, cfg)
        mock_dispatcher.resolve_and_create.assert_called_once_with(
            org_config=cfg,
            db=mock_db,
            surreal=mock_surreal_pool.get_or_create.return_value,
            falkordb_client=None,
        )
        assert result is mock_dispatcher.resolve_and_create.return_value

    async def test_surrealdb_backend_without_pool(
        self,
        mock_db: MagicMock,
        mock_dispatcher: MagicMock,
    ) -> None:
        """SurrealDB configured but no pool in ctx → fails loud (no fallback)."""
        cfg = MagicMock()
        cfg.graph_backend = "surrealdb"

        with patch("workers.backend._resolve_org_config", AsyncMock(return_value=cfg)):
            from workers.backend import resolve_graph_backend

            ctx = {"graph_backend_dispatcher": mock_dispatcher}
            with pytest.raises(
                GraphBackendUnavailableError,
                match="no connection is available",
            ):
                await resolve_graph_backend(ctx, self.ORG_ID, mock_db)

        mock_dispatcher.resolve_and_create.assert_not_called()

    async def test_falkordb_client_passed_through(
        self,
        mock_db: MagicMock,
        mock_org_config: MagicMock,
        mock_dispatcher: MagicMock,
    ) -> None:
        """``falkordb_client`` in ctx → forwarded to dispatcher."""
        falkordb_client = MagicMock()

        with patch("workers.backend._resolve_org_config", AsyncMock(return_value=mock_org_config)):
            from workers.backend import resolve_graph_backend

            ctx = {
                "graph_backend_dispatcher": mock_dispatcher,
                "falkordb_client": falkordb_client,
            }
            result = await resolve_graph_backend(ctx, self.ORG_ID, mock_db)

        mock_dispatcher.resolve_and_create.assert_called_once_with(
            org_config=mock_org_config,
            db=mock_db,
            surreal=None,
            falkordb_client=falkordb_client,
        )
        assert result is mock_dispatcher.resolve_and_create.return_value

    # ── SurrealDB pool errors ─────────────────────────────────────────────

    async def test_surrealdb_pool_raises_graph_backend_error_propagates(
        self,
        mock_db: MagicMock,
        mock_dispatcher: MagicMock,
    ) -> None:
        """Surreal pool raises ``GraphBackendUnavailableError`` → propagates."""
        cfg = MagicMock()
        cfg.graph_backend = "surrealdb"
        original_error = GraphBackendUnavailableError("SurrealDB down")
        pool = AsyncMock()
        pool.get_or_create = AsyncMock(side_effect=original_error)

        with patch("workers.backend._resolve_org_config", AsyncMock(return_value=cfg)):
            from workers.backend import resolve_graph_backend

            ctx = {
                "graph_backend_dispatcher": mock_dispatcher,
                "surreal_connection_pool": pool,
            }
            with pytest.raises(GraphBackendUnavailableError, match="SurrealDB down"):
                await resolve_graph_backend(ctx, self.ORG_ID, mock_db)

    async def test_surrealdb_pool_raises_generic_error_wraps(
        self,
        mock_db: MagicMock,
        mock_dispatcher: MagicMock,
    ) -> None:
        """Surreal pool raises generic ``Exception`` → wrapped as ``GraphBackendUnavailableError``."""
        cfg = MagicMock()
        cfg.graph_backend = "surrealdb"
        pool = AsyncMock()
        pool.get_or_create = AsyncMock(side_effect=ConnectionError("timeout"))

        with patch("workers.backend._resolve_org_config", AsyncMock(return_value=cfg)):
            from workers.backend import resolve_graph_backend

            ctx = {
                "graph_backend_dispatcher": mock_dispatcher,
                "surreal_connection_pool": pool,
            }
            with pytest.raises(GraphBackendUnavailableError, match="SurrealDB connection failed"):
                await resolve_graph_backend(ctx, self.ORG_ID, mock_db)

    # ── Dispatcher resolution failure ─────────────────────────────────────

    async def test_dispatcher_fails_raises(
        self,
        mock_db: MagicMock,
        mock_org_config: MagicMock,
        mock_dispatcher: MagicMock,
    ) -> None:
        """Dispatcher raises ``ValueError`` → ``GraphBackendUnavailableError`` (unknown backend)."""
        mock_dispatcher.resolve_and_create.side_effect = ValueError("kaboom")

        with patch("workers.backend._resolve_org_config", AsyncMock(return_value=mock_org_config)):
            from workers.backend import resolve_graph_backend

            ctx = {"graph_backend_dispatcher": mock_dispatcher}
            with pytest.raises(
                GraphBackendUnavailableError,
                match=r"Unknown graph backend 'postgres'.*kaboom",
            ):
                await resolve_graph_backend(ctx, self.ORG_ID, mock_db)

    # ── Dispatcher resolves to None ───────────────────────────────────────

    async def test_dispatcher_resolves_to_none_raises(
        self,
        mock_db: MagicMock,
        mock_org_config: MagicMock,
        mock_dispatcher: MagicMock,
    ) -> None:
        """Dispatcher returns ``None`` for a configured backend → raises."""
        mock_dispatcher.resolve_and_create.return_value = None

        with patch("workers.backend._resolve_org_config", AsyncMock(return_value=mock_org_config)):
            from workers.backend import resolve_graph_backend

            ctx = {"graph_backend_dispatcher": mock_dispatcher}
            with pytest.raises(
                GraphBackendUnavailableError,
                match="resolved to None",
            ):
                await resolve_graph_backend(ctx, self.ORG_ID, mock_db)


# ═══════════════════════════════════════════════════════════════════════════════
# _resolve_org_config  (private helper)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestResolveOrgConfig:
    """Branch coverage for ``_resolve_org_config``.

    Because the function uses inline ``import`` inside the try block, we patch
    at the real module paths that the imports resolve to at runtime.
    """

    ORG_ID: UUID = uuid4()

    @pytest.fixture
    def mock_db(self) -> MagicMock:
        return MagicMock()

    @pytest.fixture
    def mock_org_config(self) -> MagicMock:
        """A ``OrgConfigBase``-like object returned by the primary path."""
        from schemas.organization_config import OrgConfigBase

        return OrgConfigBase(graph_backend="postgres")

    # ── Primary path: bao_client in ctx ───────────────────────────────────

    async def test_primary_path_with_bao_client(
        self,
        mock_db: MagicMock,
        mock_org_config: MagicMock,
    ) -> None:
        """Primary path with ``bao_client`` in ctx → calls ``get_org_config`` and returns result.

        ``BootstrapSettings`` is never instantiated because the function
        short-circuits when ``bao_client`` is already in ``ctx``.
        """
        bao_client = MagicMock()
        mock_get = AsyncMock(return_value=mock_org_config)

        with patch("core.org_config.get_org_config", mock_get):
            from workers.backend import _resolve_org_config

            ctx = {"openbao_client": bao_client, "redis": MagicMock()}
            result = await _resolve_org_config(ctx, self.ORG_ID, mock_db)

        mock_get.assert_awaited_once_with(self.ORG_ID, redis=ctx["redis"], bao_client=bao_client)
        assert result is mock_org_config
        assert result.graph_backend == "postgres"

    async def test_primary_path_without_bao_client(
        self,
        mock_db: MagicMock,
        mock_org_config: MagicMock,
    ) -> None:
        """Primary path without ``bao_client`` → creates a short-lived client.

        Patching at the real module paths because ``BootstrapSettings`` and
        ``OpenBaoClient`` are imported *inside* the function body.
        """
        mock_bao_client = AsyncMock()
        mock_bao_client.__aenter__.return_value = mock_bao_client
        mock_bao_client.__aexit__.return_value = None

        with (
            patch("core.org_config.get_org_config", AsyncMock(return_value=mock_org_config)),
            patch("core.config.BootstrapSettings") as mock_bootstrap,
            patch("core.openbao.OpenBaoClient", return_value=mock_bao_client),
        ):
            from workers.backend import _resolve_org_config

            ctx = {"redis": MagicMock()}
            result = await _resolve_org_config(ctx, self.ORG_ID, mock_db)

        mock_bootstrap.assert_called_once()
        assert result is mock_org_config
        assert result.graph_backend == "postgres"

    async def test_primary_path_passes_redis_to_get_org_config(
        self,
        mock_db: MagicMock,
        mock_org_config: MagicMock,
    ) -> None:
        """Redis client from ctx is forwarded to ``get_org_config``."""
        redis_client = MagicMock()
        bao_client = MagicMock()

        with patch("core.org_config.get_org_config", AsyncMock(return_value=mock_org_config)) as mock_get:
            from workers.backend import _resolve_org_config

            ctx = {"openbao_client": bao_client, "redis": redis_client}
            result = await _resolve_org_config(ctx, self.ORG_ID, mock_db)

        mock_get.assert_awaited_once_with(self.ORG_ID, redis=redis_client, bao_client=bao_client)
        assert result is mock_org_config

    # ── Primary path: fallthrough on ImportError ──────────────────────────

    async def test_primary_path_import_error_falls_through_to_db(
        self,
        mock_db: MagicMock,
    ) -> None:
        """Primary path raises ``ImportError`` → falls to DB fallback."""
        from schemas.organization_config import OrgConfigBase

        expected = OrgConfigBase(graph_backend="postgres")

        with (
            patch("core.org_config.get_org_config", side_effect=ImportError("no module")),
            patch("repositories.organization_repository.OrganizationRepository") as mock_repo_cls,
        ):
            mock_repo = MagicMock()
            mock_repo_cls.return_value = mock_repo
            mock_repo.get_config = AsyncMock(return_value={"graph_backend": "postgres"})

            from workers.backend import _resolve_org_config

            ctx = {"openbao_client": MagicMock(), "redis": MagicMock()}
            result = await _resolve_org_config(ctx, self.ORG_ID, mock_db)

        assert isinstance(result, OrgConfigBase)
        assert result.graph_backend == "postgres"

    async def test_primary_path_exception_falls_through_to_db(
        self,
        mock_db: MagicMock,
    ) -> None:
        """Primary path raises generic ``Exception`` → falls to DB fallback."""
        from schemas.organization_config import OrgConfigBase

        expected = OrgConfigBase(graph_backend="surrealdb")

        with (
            patch("core.org_config.get_org_config", side_effect=RuntimeError("bao down")),
            patch("repositories.organization_repository.OrganizationRepository") as mock_repo_cls,
        ):
            mock_repo = MagicMock()
            mock_repo_cls.return_value = mock_repo
            mock_repo.get_config = AsyncMock(return_value={"graph_backend": "surrealdb"})

            from workers.backend import _resolve_org_config

            ctx = {"openbao_client": MagicMock()}
            result = await _resolve_org_config(ctx, self.ORG_ID, mock_db)

        assert isinstance(result, OrgConfigBase)
        assert result.graph_backend == "surrealdb"

    # ── DB fallback path ──────────────────────────────────────────────────

    async def test_db_fallback_empty_config_returns_all_none(
        self,
        mock_db: MagicMock,
    ) -> None:
        """DB returns empty config → ``OrgConfigBase`` with all fields ``None``."""
        from schemas.organization_config import OrgConfigBase

        with (
            patch("core.org_config.get_org_config", side_effect=ImportError("no module")),
            patch("repositories.organization_repository.OrganizationRepository") as mock_repo_cls,
        ):
            mock_repo = MagicMock()
            mock_repo_cls.return_value = mock_repo
            mock_repo.get_config = AsyncMock(return_value={})

            from workers.backend import _resolve_org_config

            ctx: dict = {}
            result = await _resolve_org_config(ctx, self.ORG_ID, mock_db)

        assert isinstance(result, OrgConfigBase)
        # All fields should be None for an empty config
        assert all(v is None for v in result.model_dump().values())

    async def test_db_fallback_succeeds(
        self,
        mock_db: MagicMock,
    ) -> None:
        """DB fallback returns a full config → correctly parsed into ``OrgConfigBase``."""
        from schemas.organization_config import OrgConfigBase

        with (
            patch("core.org_config.get_org_config", side_effect=ImportError("no module")),
            patch("repositories.organization_repository.OrganizationRepository") as mock_repo_cls,
        ):
            mock_repo = MagicMock()
            mock_repo_cls.return_value = mock_repo
            mock_repo.get_config = AsyncMock(
                return_value={"graph_backend": "postgres", "graph_max_traversal_depth": 5},
            )

            from workers.backend import _resolve_org_config

            ctx: dict = {}
            result = await _resolve_org_config(ctx, self.ORG_ID, mock_db)

        assert isinstance(result, OrgConfigBase)
        assert result.graph_backend == "postgres"
        assert result.graph_max_traversal_depth == 5

    # ── Both paths fail ───────────────────────────────────────────────────

    async def test_both_paths_fail_returns_none(
        self,
        mock_db: MagicMock,
    ) -> None:
        """Primary raises, DB fallback also raises → returns ``None``."""
        with (
            patch("core.org_config.get_org_config", side_effect=ImportError("no module")),
            patch("repositories.organization_repository.OrganizationRepository") as mock_repo_cls,
        ):
            mock_repo = MagicMock()
            mock_repo_cls.return_value = mock_repo
            mock_repo.get_config = AsyncMock(side_effect=RuntimeError("db unreachable"))

            from workers.backend import _resolve_org_config

            ctx: dict = {}
            result = await _resolve_org_config(ctx, self.ORG_ID, mock_db)

        assert result is None

    async def test_primary_fails_with_generic_and_db_fails_too_returns_none(
        self,
        mock_db: MagicMock,
    ) -> None:
        """Generic exception in primary, then DB fallback fails → returns ``None``."""
        with (
            patch("core.org_config.get_org_config", side_effect=RuntimeError("bao timeout")),
            patch("repositories.organization_repository.OrganizationRepository") as mock_repo_cls,
        ):
            mock_repo = MagicMock()
            mock_repo_cls.return_value = mock_repo
            mock_repo.get_config = AsyncMock(side_effect=ValueError("bad query"))

            from workers.backend import _resolve_org_config

            ctx: dict = {}
            result = await _resolve_org_config(ctx, self.ORG_ID, mock_db)

        assert result is None
