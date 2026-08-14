"""Unit tests for dependencies/services.py — service factory functions."""

from __future__ import annotations

from unittest.mock import ANY, AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import Request, Depends
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.unit

ORG_ID_STR = "00000000-0000-0000-0000-000000000001"
ORG_UUID = UUID(ORG_ID_STR)


# ── Webhook Service ─────────────────────────────────────────────────────────────


class TestGetWebhookService:
    """get_webhook_service: factory for WebhookService."""

    @pytest.mark.asyncio
    async def test_creates_webhook_service_with_repo(self) -> None:
        """Returns WebhookService initialised with WebhookRepository."""
        from dependencies.services import get_webhook_service

        db = AsyncMock(spec=AsyncSession)

        with (
            patch("dependencies.services.WebhookRepository") as mock_repo_cls,
            patch("dependencies.services.WebhookService") as mock_svc_cls,
        ):
            mock_repo = MagicMock()
            mock_repo_cls.return_value = mock_repo
            mock_svc_cls.return_value = "webhook_service"

            result = await get_webhook_service(db)

            mock_repo_cls.assert_called_once_with(db)
            mock_svc_cls.assert_called_once_with(repo=mock_repo)
            assert result == "webhook_service"


# ── User Service ────────────────────────────────────────────────────────────────


class TestGetUserService:
    """get_user_service: factory for UserService."""

    @pytest.mark.asyncio
    async def test_creates_user_service_with_repo_and_webhook(self) -> None:
        from dependencies.services import get_user_service

        db = AsyncMock(spec=AsyncSession)
        webhook = MagicMock()

        with (
            patch("dependencies.services.UserRepository") as mock_repo_cls,
            patch("dependencies.services.UserService") as mock_svc_cls,
        ):
            mock_repo = MagicMock()
            mock_repo_cls.return_value = mock_repo
            mock_svc_cls.return_value = "user_service"

            result = await get_user_service(db, webhook)

            mock_repo_cls.assert_called_once_with(db)
            mock_svc_cls.assert_called_once_with(
                repo=mock_repo, webhook_service=webhook
            )
            assert result == "user_service"


# ── Session Service ──────────────────────────────────────────────────────────────


class TestGetSessionService:
    """get_session_service: factory for SessionService."""

    @pytest.mark.asyncio
    async def test_creates_session_service(self) -> None:
        from dependencies.services import get_session_service

        db = AsyncMock(spec=AsyncSession)
        webhook = MagicMock()

        with (
            patch("dependencies.services.SessionRepository") as mock_repo_cls,
            patch("dependencies.services.SessionService") as mock_svc_cls,
        ):
            mock_repo = MagicMock()
            mock_repo_cls.return_value = mock_repo
            mock_svc_cls.return_value = "session_service"

            result = await get_session_service(db, webhook)

            mock_repo_cls.assert_called_once_with(db)
            mock_svc_cls.assert_called_once_with(
                repo=mock_repo, webhook_service=webhook
            )
            assert result == "session_service"


# ── Auth Service ────────────────────────────────────────────────────────────────


class TestGetAuthService:
    """get_auth_service: factory for AuthService (needs Redis, Email, OTP, Bao)."""

    @pytest.mark.asyncio
    async def test_creates_auth_service_with_all_deps(self) -> None:
        from dependencies.services import get_auth_service

        request = MagicMock(spec=Request)
        request.app.state.redis = AsyncMock()
        request.app.state.openbao_client = MagicMock()

        db = AsyncMock(spec=AsyncSession)

        with (
            patch("dependencies.services.EmailConfig") as mock_email_config_cls,
            patch("dependencies.services.EmailService") as mock_email_svc_cls,
            patch("dependencies.services.OtpService") as mock_otp_svc_cls,
            patch("dependencies.services.AuthRepository") as mock_repo_cls,
            patch("dependencies.services.AuthService") as mock_svc_cls,
        ):
            mock_email_config = MagicMock()
            mock_email_config_cls.from_settings.return_value = mock_email_config
            mock_email = MagicMock()
            mock_email_svc_cls.return_value = mock_email
            mock_otp = MagicMock()
            mock_otp_svc_cls.return_value = mock_otp
            mock_repo = MagicMock()
            mock_repo_cls.return_value = mock_repo
            mock_svc_cls.return_value = "auth_service"

            result = await get_auth_service(request, db)

            mock_email_config_cls.from_settings.assert_called_once()
            mock_email_svc_cls.assert_called_once_with(mock_email_config)
            mock_otp_svc_cls.assert_called_once_with(
                redis=request.app.state.redis, email_service=mock_email
            )
            mock_repo_cls.assert_called_once_with(db)
            mock_svc_cls.assert_called_once_with(
                repo=mock_repo,
                otp_service=mock_otp,
                redis=request.app.state.redis,
                org_repo=mock_svc_cls.call_args.kwargs["org_repo"],
                email_service=mock_email,
                bao_client=request.app.state.openbao_client,
            )
            assert result == "auth_service"

    @pytest.mark.asyncio
    async def test_raises_when_redis_missing(self) -> None:
        """No redis on app.state → RuntimeError."""
        from dependencies.services import get_auth_service

        request = MagicMock(spec=Request)
        request.app.state.redis = None
        db = AsyncMock(spec=AsyncSession)

        with pytest.raises(RuntimeError, match="Redis client not found"):
            await get_auth_service(request, db)


# ── Org Request Service ─────────────────────────────────────────────────────────


class TestGetOrgRequestService:
    """get_org_request_service: factory for OrgRequestService (needs Bao + Redis)."""

    @pytest.mark.asyncio
    async def test_creates_org_request_service_with_all_deps(self) -> None:
        from dependencies.services import get_org_request_service

        request = MagicMock(spec=Request)
        request.app.state.redis = AsyncMock()
        request.app.state.openbao_client = MagicMock()

        db = AsyncMock(spec=AsyncSession)
        auth_service = MagicMock()

        with (
            patch("dependencies.services.EmailConfig") as mock_email_config_cls,
            patch("dependencies.services.EmailService") as mock_email_svc_cls,
            patch("dependencies.services.OtpService") as mock_otp_svc_cls,
            patch("dependencies.services.OrganizationRepository") as mock_org_repo_cls,
            patch("dependencies.services.OrganizationService") as mock_org_svc_cls,
            patch("dependencies.services.OrgRequestService") as mock_svc_cls,
        ):
            mock_email_config = MagicMock()
            mock_email_config_cls.from_settings.return_value = mock_email_config
            mock_email = MagicMock()
            mock_email_svc_cls.return_value = mock_email
            mock_otp = MagicMock()
            mock_otp_svc_cls.return_value = mock_otp
            mock_org_repo = MagicMock()
            mock_org_repo_cls.return_value = mock_org_repo
            mock_org_svc = MagicMock()
            mock_org_svc_cls.return_value = mock_org_svc
            mock_svc_cls.return_value = "org_request_service"

            result = await get_org_request_service(request, db, auth_service)

            mock_otp_svc_cls.assert_called_once_with(
                redis=request.app.state.redis, email_service=mock_email
            )
            mock_org_svc_cls.assert_called_once_with(
                repo=mock_org_repo, bao_client=request.app.state.openbao_client
            )
            mock_svc_cls.assert_called_once_with(
                db=db,
                auth_service=auth_service,
                org_service=mock_org_svc,
                otp_service=mock_otp,
                redis=request.app.state.redis,
                bao_client=request.app.state.openbao_client,
            )
            assert result == "org_request_service"

    @pytest.mark.asyncio
    async def test_raises_when_redis_missing(self) -> None:
        """No redis on app.state → RuntimeError before service construction."""
        from dependencies.services import get_org_request_service

        request = MagicMock(spec=Request)
        request.app.state.redis = None
        request.app.state.openbao_client = MagicMock()
        db = AsyncMock(spec=AsyncSession)
        auth_service = MagicMock()

        with (
            pytest.raises(RuntimeError, match="Redis client not found"),
            patch("dependencies.services.OrgRequestService") as mock_svc_cls,
        ):
            await get_org_request_service(request, db, auth_service)

        mock_svc_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_raises_when_openbao_missing(self) -> None:
        """No openbao_client on app.state → RuntimeError before service construction."""
        from dependencies.services import get_org_request_service

        request = MagicMock(spec=Request)
        request.app.state.redis = AsyncMock()
        request.app.state.openbao_client = None
        db = AsyncMock(spec=AsyncSession)
        auth_service = MagicMock()

        with (
            pytest.raises(RuntimeError, match="OpenBao client not found"),
            patch("dependencies.services.OrgRequestService") as mock_svc_cls,
        ):
            await get_org_request_service(request, db, auth_service)

        mock_svc_cls.assert_not_called()


# ── Fact Service ────────────────────────────────────────────────────────────────


class TestGetFactService:
    """get_fact_service: factory for FactService."""

    @pytest.mark.asyncio
    async def test_creates_fact_service(self) -> None:
        from dependencies.services import get_fact_service

        request = MagicMock(spec=Request)
        request.app.state.redis = AsyncMock()
        db = AsyncMock(spec=AsyncSession)
        webhook = MagicMock()

        with (
            patch("dependencies.services.FactRepository") as mock_fact_repo_cls,
            patch("dependencies.services.SessionRepository") as mock_sess_repo_cls,
            patch("dependencies.services.FactService") as mock_svc_cls,
        ):
            mock_fact_repo = MagicMock()
            mock_fact_repo_cls.return_value = mock_fact_repo
            mock_sess_repo = MagicMock()
            mock_sess_repo_cls.return_value = mock_sess_repo
            mock_svc_cls.return_value = "fact_service"

            result = await get_fact_service(request, db, webhook)

            mock_fact_repo_cls.assert_called_once_with(db)
            mock_sess_repo_cls.assert_called_once_with(db)
            mock_svc_cls.assert_called_once_with(
                db=db,
                redis_client=request.app.state.redis,
                fact_repo=mock_fact_repo,
                session_repo=mock_sess_repo,
                webhook_service=webhook,
                graph_backend_resolver=ANY,
            )
            resolver = mock_svc_cls.call_args.kwargs["graph_backend_resolver"]
            assert callable(resolver)
            assert result == "fact_service"

    @pytest.mark.asyncio
    async def test_handles_none_redis(self) -> None:
        """When redis is None on app.state, passes None to service."""
        from dependencies.services import get_fact_service

        request = MagicMock(spec=Request)
        request.app.state.redis = None
        db = AsyncMock(spec=AsyncSession)
        webhook = MagicMock()

        with (
            patch("dependencies.services.FactRepository"),
            patch("dependencies.services.SessionRepository"),
            patch("dependencies.services.FactService") as mock_svc_cls,
        ):
            mock_svc_cls.return_value = "fact_service"

            result = await get_fact_service(request, db, webhook)
            assert result == "fact_service"
            # redis_client was None
            call_kwargs = mock_svc_cls.call_args.kwargs
            assert call_kwargs["redis_client"] is None


# ── Memory Service ──────────────────────────────────────────────────────────────


class TestGetMemoryService:
    """get_memory_service: factory for MemoryService."""

    @pytest.mark.asyncio
    async def test_creates_memory_service(self) -> None:
        from dependencies.services import get_memory_service

        request = MagicMock(spec=Request)
        request.app.state.redis = AsyncMock()
        db = AsyncMock(spec=AsyncSession)
        webhook = MagicMock()

        repos_patch = {
            "EpisodeRepository": MagicMock(),
            "SessionRepository": MagicMock(),
            "UserRepository": MagicMock(),
            "FactRepository": MagicMock(),
            "OrganizationRepository": MagicMock(),
            "EpisodeBlobRepository": MagicMock(),
        }

        with (
            patch("dependencies.services.EpisodeRepository") as m1,
            patch("dependencies.services.SessionRepository") as m2,
            patch("dependencies.services.UserRepository") as m3,
            patch("dependencies.services.FactRepository") as m4,
            patch("dependencies.services.OrganizationRepository") as m5,
            patch("dependencies.services.EpisodeBlobRepository") as m6,
            patch("dependencies.services.MemoryService") as mock_svc_cls,
        ):
            for m in (m1, m2, m3, m4, m5, m6):
                m.return_value = MagicMock()
            mock_svc_cls.return_value = "memory_service"

            result = await get_memory_service(request, db, webhook)

            mock_svc_cls.assert_called_once_with(
                db=db,
                redis_client=request.app.state.redis,
                episode_repo=m1.return_value,
                session_repo=m2.return_value,
                user_repo=m3.return_value,
                fact_repo=m4.return_value,
                org_repo=m5.return_value,
                webhook_service=webhook,
                blob_repo=m6.return_value,
            )
            assert result == "memory_service"

    @pytest.mark.asyncio
    async def test_raises_when_redis_missing(self) -> None:
        """No redis on app.state → RuntimeError."""
        from dependencies.services import get_memory_service

        request = MagicMock(spec=Request)
        request.app.state.redis = None
        db = AsyncMock(spec=AsyncSession)
        webhook = MagicMock()

        with pytest.raises(RuntimeError, match="Redis client not found"):
            await get_memory_service(request, db, webhook)


# ── Graph Service ───────────────────────────────────────────────────────────────


class TestGetGraphService:
    """get_graph_service: factory for GraphService (complex — SurrealDB, FalkorDB, dispatcher)."""

    @pytest.fixture
    def mock_dispatcher(self) -> MagicMock:
        dispatcher = MagicMock()
        dispatcher.resolve_and_create.return_value = MagicMock()
        return dispatcher

    @pytest.fixture
    def mock_org_config(self) -> MagicMock:
        cfg = MagicMock()
        cfg.graph_backend = "postgres"
        cfg.falkordb_url = None
        return cfg

    @pytest.mark.asyncio
    async def test_creates_graph_service_postgres(
        self, mock_dispatcher: MagicMock, mock_org_config: MagicMock
    ) -> None:
        """Postgres backend → no Surreal/Falkor connections, basic service."""
        from dependencies.services import get_graph_service

        request = MagicMock(spec=Request)
        request.state.org_id = ORG_ID_STR
        request.app.state.graph_backend_dispatcher = mock_dispatcher
        request.app.state.surreal_connection_pool = None

        db = AsyncMock(spec=AsyncSession)
        webhook = MagicMock()

        with (
            patch("dependencies.services.UserRepository") as mock_user_repo_cls,
            patch("dependencies.services.FactRepository") as mock_fact_repo_cls,
            patch("dependencies.services.GraphService") as mock_svc_cls,
        ):
            mock_user_repo = MagicMock()
            mock_user_repo_cls.return_value = mock_user_repo
            mock_fact_repo = MagicMock()
            mock_fact_repo_cls.return_value = mock_fact_repo
            mock_svc_cls.return_value = "graph_service"

            result = await get_graph_service(request, mock_org_config, db, webhook)

            mock_dispatcher.resolve_and_create.assert_called_once()
            mock_svc_cls.assert_called_once_with(
                graph_backend=mock_dispatcher.resolve_and_create.return_value,
                user_repo=mock_user_repo,
                fact_repo=mock_fact_repo,
                webhook_service=webhook,
            )
            assert result == "graph_service"

    @pytest.mark.asyncio
    async def test_surrealdb_backend_uses_pool(
        self, mock_dispatcher: MagicMock
    ) -> None:
        """graph_backend=surrealdb → attempts pool.get_or_create."""
        from dependencies.services import get_graph_service

        mock_org_config = MagicMock()
        mock_org_config.graph_backend = "surrealdb"
        mock_org_config.falkordb_url = None

        request = MagicMock(spec=Request)
        request.state.org_id = ORG_ID_STR
        request.app.state.graph_backend_dispatcher = mock_dispatcher

        mock_pool = MagicMock()
        mock_pool.get_or_create = AsyncMock(return_value=MagicMock())
        request.app.state.surreal_connection_pool = mock_pool

        db = AsyncMock(spec=AsyncSession)
        webhook = MagicMock()

        with (
            patch("dependencies.services.UserRepository"),
            patch("dependencies.services.FactRepository"),
            patch("dependencies.services.GraphService"),
            patch("dependencies.services.get_settings") as mock_get_settings,
        ):
            mock_settings = MagicMock()
            mock_settings.SURREALDB_URL = "ws://surrealdb:8000/rpc"
            mock_get_settings.return_value = mock_settings

            await get_graph_service(request, mock_org_config, db, webhook)

            mock_pool.get_or_create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_surrealdb_pool_none_fails_loud(
        self, mock_dispatcher: MagicMock, mock_org_config: MagicMock
    ) -> None:
        """graph_backend=surrealdb but pool is None → fail loud (no doomed backend)."""
        from core.exceptions import GraphBackendUnavailableError
        from dependencies.services import get_graph_service

        mock_org_config = MagicMock()
        mock_org_config.graph_backend = "surrealdb"
        mock_org_config.falkordb_url = None

        request = MagicMock(spec=Request)
        request.state.org_id = ORG_ID_STR
        request.app.state.graph_backend_dispatcher = mock_dispatcher
        request.app.state.surreal_connection_pool = None

        db = AsyncMock(spec=AsyncSession)
        webhook = MagicMock()

        with (
            patch("dependencies.services.UserRepository"),
            patch("dependencies.services.FactRepository"),
            patch("dependencies.services.GraphService"),
        ):
            # A configured-but-unreachable backend is a broken backend, not a
            # disabled one — constructing GraphService with a doomed
            # SurrealGraphBackend(surreal=None) would 500 on every query.
            with pytest.raises(
                GraphBackendUnavailableError, match="no connection is available"
            ):
                await get_graph_service(request, mock_org_config, db, webhook)

            mock_dispatcher.resolve_and_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_falkordb_per_org_config(
        self, mock_dispatcher: MagicMock
    ) -> None:
        """falkordb_url in org_config → creates per-org FalkorDB client."""
        from dependencies.services import get_graph_service

        mock_org_config = MagicMock()
        mock_org_config.graph_backend = "postgres"
        mock_org_config.falkordb_url = "redis://falkordb:6379"

        request = MagicMock(spec=Request)
        request.state.org_id = ORG_ID_STR
        request.app.state.graph_backend_dispatcher = mock_dispatcher
        request.app.state.surreal_connection_pool = None
        request.app.state.falkordb_client = None

        db = AsyncMock(spec=AsyncSession)
        webhook = MagicMock()

        # FalkorDB and AsyncBlockingConnectionPool are imported locally inside
        # get_graph_service(), so patch the real source modules.
        with (
            patch("dependencies.services.UserRepository"),
            patch("dependencies.services.FactRepository"),
            patch("dependencies.services.GraphService"),
            patch("falkordb.asyncio.FalkorDB") as mock_falkor_cls,
            patch(
                "redis.asyncio.BlockingConnectionPool"
            ) as mock_pool_cls,
        ):
            mock_pool = MagicMock()
            mock_pool_cls.from_url.return_value = mock_pool
            mock_falkor = MagicMock()
            mock_falkor_cls.return_value = mock_falkor

            await get_graph_service(request, mock_org_config, db, webhook)

            mock_pool_cls.from_url.assert_called_once_with(
                mock_org_config.falkordb_url,
                max_connections=5,
                socket_timeout=10,
                socket_keepalive=True,
                decode_responses=True,
            )
            mock_falkor_cls.assert_called_once_with(connection_pool=mock_pool)
            # dispatcher got the falkordb_client
            call_kwargs = mock_dispatcher.resolve_and_create.call_args.kwargs
            assert call_kwargs["falkordb_client"] is mock_falkor


# ── Auth Throttle ────────────────────────────────────────────────────────────────


class TestGetAuthThrottle:
    """get_auth_throttle: factory for AuthThrottle."""

    @pytest.mark.asyncio
    async def test_creates_auth_throttle(self) -> None:
        from dependencies.services import get_auth_throttle

        request = MagicMock(spec=Request)
        request.app.state.redis = AsyncMock()

        with patch("dependencies.services.AuthThrottle") as mock_cls:
            from core.config import get_settings

            settings = get_settings()
            mock_cls.return_value = "auth_throttle"

            result = await get_auth_throttle(request)

            mock_cls.assert_called_once_with(
                redis=request.app.state.redis,
                login_max_per_ip=settings.RATE_LIMIT_IP_MAX,
                login_window_sec=settings.RATE_LIMIT_WINDOW_SEC,
            )
            assert result == "auth_throttle"

    @pytest.mark.asyncio
    async def test_raises_when_redis_missing(self) -> None:
        from dependencies.services import get_auth_throttle

        request = MagicMock(spec=Request)
        request.app.state.redis = None

        with pytest.raises(RuntimeError, match="Redis client not found"):
            await get_auth_throttle(request)


# ── Quick Actions Service ────────────────────────────────────────────────────────


class TestGetQuickActionsService:
    """get_quick_actions_service: factory for QuickActionsService."""

    @pytest.mark.asyncio
    async def test_creates_quick_actions_service(self) -> None:
        from dependencies.services import get_quick_actions_service

        db = AsyncMock(spec=AsyncSession)

        with (
            patch("dependencies.services.ProjectRepository") as m1,
            patch("dependencies.services.UserRepository") as m2,
            patch("dependencies.services.OrganizationRepository") as m3,
            patch("dependencies.services.QuickActionsService") as mock_svc_cls,
        ):
            for m in (m1, m2, m3):
                m.return_value = MagicMock()
            mock_svc_cls.return_value = "quick_actions_svc"

            result = await get_quick_actions_service(db)

            mock_svc_cls.assert_called_once_with(
                project_repo=m1.return_value,
                user_repo=m2.return_value,
                org_repo=m3.return_value,
            )
            assert result == "quick_actions_svc"


# ── Graph Backend (read-only) ────────────────────────────────────────────────────


class TestGetGraphBackendForProject:
    """get_graph_backend_for_project: resolves and returns a project-scoped GraphBackend."""

    @pytest.mark.asyncio
    async def test_resolves_backend_without_service(self) -> None:
        from dependencies.services import get_graph_backend_for_project

        dispatcher = MagicMock()
        dispatcher.resolve_and_create.return_value = "graph_backend_instance"

        request = MagicMock(spec=Request)
        request.state.org_id = ORG_ID_STR
        request.app.state.graph_backend_dispatcher = dispatcher
        request.app.state.surreal_connection_pool = None
        request.app.state.falkordb_client = None

        org_config = MagicMock()
        org_config.graph_backend = "postgres"
        org_config.falkordb_url = None

        db = AsyncMock(spec=AsyncSession)

        result = await get_graph_backend_for_project(request, org_config, db)

        dispatcher.resolve_and_create.assert_called_once()
        assert result == "graph_backend_instance"

    @pytest.mark.asyncio
    async def test_surrealdb_with_pool(self) -> None:
        """SurrealDB backend with pool → uses pool.get_or_create."""
        from dependencies.services import get_graph_backend_for_project

        dispatcher = MagicMock()
        dispatcher.resolve_and_create.return_value = MagicMock()

        request = MagicMock(spec=Request)
        request.state.org_id = ORG_ID_STR
        request.app.state.graph_backend_dispatcher = dispatcher

        mock_pool = MagicMock()
        mock_pool.get_or_create = AsyncMock(return_value=MagicMock())
        request.app.state.surreal_connection_pool = mock_pool

        org_config = MagicMock()
        org_config.graph_backend = "surrealdb"
        org_config.falkordb_url = None

        db = AsyncMock(spec=AsyncSession)

        with patch(
            "dependencies.services.get_settings"
        ) as mock_get_settings:
            mock_settings = MagicMock()
            mock_settings.SURREALDB_URL = "ws://surrealdb:8000/rpc"
            mock_get_settings.return_value = mock_settings

            await get_graph_backend_for_project(request, org_config, db)

            mock_pool.get_or_create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_surrealdb_pool_none_fails_loud(self) -> None:
        """SurrealDB backend but pool is None → fail loud, dispatcher not called."""
        from core.exceptions import GraphBackendUnavailableError
        from dependencies.services import get_graph_backend_for_project

        dispatcher = MagicMock()
        dispatcher.resolve_and_create.return_value = "backend"

        request = MagicMock(spec=Request)
        request.state.org_id = ORG_ID_STR
        request.app.state.graph_backend_dispatcher = dispatcher
        request.app.state.surreal_connection_pool = None

        org_config = MagicMock()
        org_config.graph_backend = "surrealdb"
        org_config.falkordb_url = None

        db = AsyncMock(spec=AsyncSession)

        # A configured-but-unreachable backend must fail loud (503) so the
        # caller never gets a backend that 500s on every query.
        with pytest.raises(
            GraphBackendUnavailableError, match="no connection is available"
        ):
            await get_graph_backend_for_project(request, org_config, db)

        dispatcher.resolve_and_create.assert_not_called()
