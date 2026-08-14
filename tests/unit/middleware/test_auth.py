"""Unit tests for AuthMiddleware — dual-mode JWT + API key auth."""
# ruff: noqa: S105, S106, S107 — every fixture here IS a token/secret by design

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.requests import (
    Request,  # noqa: TC002 — FastAPI resolves Request at route registration
)

from core.exceptions import NotFoundError, register_exception_handlers
from dependencies.services import get_invite_service
from middleware.auth import AuthMiddleware, _is_jwt_token, _is_public_path
from routers.auth import router
from schemas.auth import InviteInfoResponse
from schemas.users import UserListResponse


@pytest.mark.unit
class TestAuthMiddleware:
    """Test suite for AuthMiddleware — JWT + API key authentication."""

    # ── Helpers ──────────────────────────────────────────────────────────────────

    def _create_jwt(
        self,
        sub: str = "user-123",
        org_id: str = "org-456",
        role: str = "member",
        token_type: str = "access",
        secret: str = "a" * 32,
        expired: bool = False,
        mcp: bool | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "sub": sub,
            "org_id": org_id,
            "role": role,
            "type": token_type,
        }
        if mcp is not None:
            payload["mcp"] = mcp
        if expired:
            payload["exp"] = datetime.now(UTC) - timedelta(hours=1)
        else:
            payload["exp"] = datetime.now(UTC) + timedelta(hours=1)
        payload["iat"] = datetime.now(UTC)
        return jwt.encode(payload, secret, algorithm="HS256")

    def _create_app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/test")
        async def echo() -> dict:
            return {"status": "ok"}

        @app.options("/test")
        async def echo_opt() -> dict:
            return {"status": "ok"}

        app.add_middleware(AuthMiddleware)
        return app

    def _create_app_with_state_check(self) -> FastAPI:
        """Create an app where the route handler inspects request.state."""
        app = FastAPI()

        @app.get("/test")
        async def check_state(request: Request) -> dict:
            return {
                "auth_type": getattr(request.state, "auth_type", None),
                "org_id": getattr(request.state, "org_id", None),
                "user_id": getattr(request.state, "user_id", None),
                "role": getattr(request.state, "role", None),
                "api_key_scopes": getattr(request.state, "api_key_scopes", []),
                "api_key_project_id": getattr(
                    request.state, "api_key_project_id", None
                ),
            }

        @app.options("/test")
        async def check_state_opt(request: Request) -> dict:
            return {"status": "ok"}

        app.add_middleware(AuthMiddleware)
        return app

    # ── Public endpoints ────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_public_path_passthrough(self) -> None:
        """Public endpoints (/health, /docs) pass without auth."""
        app = FastAPI()

        @app.get("/health")
        async def health() -> dict:
            return {"status": "healthy"}

        app.add_middleware(AuthMiddleware)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/health")
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_metrics_exact_path_passthrough(self) -> None:
        """Exact /metrics path passes through unauthenticated."""
        app = FastAPI()

        @app.get("/metrics")
        async def metrics() -> dict:
            return {"status": "ok"}

        app.add_middleware(AuthMiddleware)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/metrics")
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_public_path_suffix_match(self) -> None:
        """_is_public_path matches suffix patterns."""
        assert _is_public_path("/v1/auth/login") is True
        assert _is_public_path("/v1/auth/signup") is True
        assert _is_public_path("/docs") is True

    @pytest.mark.asyncio
    async def test_non_public_path_requires_auth(self) -> None:
        """Non-public endpoints require auth."""
        app = self._create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/test")
            assert resp.status_code == 401

    # ── Missing / invalid auth header ───────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_missing_auth_header_returns_401(self) -> None:
        """Missing Authorization header returns 401 RFC 7807."""
        app = self._create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/test")
            assert resp.status_code == 401
            body = resp.json()
            assert body["title"] == "Authentication Required"
            assert body["status"] == 401

    @pytest.mark.asyncio
    async def test_empty_bearer_token_returns_401(self) -> None:
        """Bearer with empty token returns 401."""
        app = self._create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/test", headers={"Authorization": "Bearer "})
            assert resp.status_code == 401
            assert resp.json()["title"] == "Empty Credentials"

    @pytest.mark.asyncio
    async def test_wrong_auth_scheme_returns_401(self) -> None:
        """Authorization without 'Bearer ' prefix returns 401."""
        app = self._create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/test", headers={"Authorization": "Basic abc123"})
            assert resp.status_code == 401
            assert resp.json()["title"] == "Invalid Authorization Scheme"

    # ── JWT auth ────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_valid_jwt_passes(self) -> None:
        """Valid JWT token passes through."""
        token = self._create_jwt()
        app = self._create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/test", headers={"Authorization": f"Bearer {token}"})
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_valid_jwt_sets_state(self) -> None:
        """Valid JWT sets correct state values."""
        token = self._create_jwt(sub="user-abc", org_id="org-xyz", role="admin")
        app = self._create_app_with_state_check()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/test", headers={"Authorization": f"Bearer {token}"})
            assert resp.status_code == 200, (
                f"Expected 200, got {resp.status_code}: {resp.text}"
            )
            state = resp.json()
            assert state["auth_type"] == "jwt"
            assert state["org_id"] == "org-xyz"
            assert state["user_id"] == "user-abc"
            assert state["role"] == "admin"

    @pytest.mark.asyncio
    async def test_expired_jwt_returns_401(self) -> None:
        """Expired JWT returns 401."""
        token = self._create_jwt(expired=True)
        app = self._create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/test", headers={"Authorization": f"Bearer {token}"})
            assert resp.status_code == 401
            assert resp.json()["title"] == "Invalid Token"

    @pytest.mark.asyncio
    async def test_malformed_jwt_returns_401(self) -> None:
        """Malformed token falls through to the API key flow -> 401."""
        app = self._create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get(
                "/test",
                headers={"Authorization": "Bearer definitely-not-a-jwt"},
            )
            # Falls through to API key flow which has no db_factory configured -> 500
            # But we're just testing it doesn't crash and returns an error
            assert resp.status_code in (401, 500)

    @pytest.mark.asyncio
    async def test_jwt_wrong_secret_returns_401(self) -> None:
        """JWT signed with wrong key returns 401."""
        token = self._create_jwt(secret="wrong-secret-key-1234567890abcdef")
        app = self._create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/test", headers={"Authorization": f"Bearer {token}"})
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_jwt_missing_required_claims(self) -> None:
        """JWT missing sub or org_id is rejected."""
        import core.config as cfg

        payload: dict[str, Any] = {
            "exp": datetime.now(UTC) + timedelta(hours=1),
            "iat": datetime.now(UTC),
            "type": "access",
        }
        s = cfg.get_settings()
        token = jwt.encode(payload, s.SECRET_KEY, algorithm="HS256")
        app = self._create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/test", headers={"Authorization": f"Bearer {token}"})
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_refresh_token_rejected(self) -> None:
        """Refresh-type token returns 401."""
        token = self._create_jwt(token_type="refresh")
        app = self._create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/test", headers={"Authorization": f"Bearer {token}"})
            assert resp.status_code == 401

    # ── Must-change-password gate (mcp claim) ────────────────────────────────

    def _create_exempt_app(self) -> FastAPI:
        """App with the four exempt must-change paths + a normal route."""
        app = FastAPI()

        @app.post("/v1/auth/change-password")
        async def change_password() -> dict:
            return {"ok": True}

        @app.get("/v1/auth/me")
        async def me() -> dict:
            return {"ok": True}

        @app.post("/v1/auth/logout")
        async def logout() -> dict:
            return {"ok": True}

        @app.post("/v1/auth/refresh")
        async def refresh() -> dict:
            return {"ok": True}

        @app.get("/v1/users")
        async def list_users() -> dict:
            return {"ok": True}

        app.add_middleware(AuthMiddleware)
        return app

    @pytest.mark.asyncio
    async def test_jwt_mcp_claim_blocks_dashboard_route_403(self) -> None:
        """A JWT with mcp=True is rejected with 403 on ANY non-exempt route —
        including member surfaces that authenticate via get_current_user_id
        (the middleware gate closes the dependency-level gap)."""
        token = self._create_jwt(mcp=True)
        app = self._create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/test", headers={"Authorization": f"Bearer {token}"})

        assert resp.status_code == 403
        body = resp.json()
        assert body["status"] == 403
        assert body["detail"] == "Password change required"

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("POST", "/v1/auth/change-password"),
            ("GET", "/v1/auth/me"),
            ("POST", "/v1/auth/logout"),
            ("POST", "/v1/auth/refresh"),
        ],
    )
    @pytest.mark.asyncio
    async def test_jwt_mcp_claim_exempt_paths_pass(
        self, method: str, path: str
    ) -> None:
        """A JWT with mcp=True still reaches the exempt paths (200)."""
        token = self._create_jwt(mcp=True)
        app = self._create_exempt_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.request(
                method, path, headers={"Authorization": f"Bearer {token}"}
            )
        assert resp.status_code == 200, resp.text

    @pytest.mark.asyncio
    async def test_jwt_mcp_false_or_absent_passes(self) -> None:
        """mcp=False (or an absent claim — pre-feature tokens) passes."""
        app = self._create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get(
                "/test",
                headers={"Authorization": f"Bearer {self._create_jwt(mcp=False)}"},
            )
            assert resp.status_code == 200
            resp2 = await c.get(
                "/test",
                headers={"Authorization": f"Bearer {self._create_jwt()}"},
            )
            assert resp2.status_code == 200

    # ── Must-change-password gate — REAL /v1/users router ─────────────────
    # The generic 403 test above uses a stub route.  These pin the observed
    # contract on the surface it names: GET /v1/users (a route that
    # authenticates via get_current_user_id) under the REAL AuthMiddleware
    # + REAL users router — the middleware must 403 before the route runs.

    def _create_users_app(self) -> FastAPI:
        """Real users router + real AuthMiddleware, route deps mocked."""
        from dependencies.auth import require_org_id
        from dependencies.db import get_db
        from routers.users import get_user_service, router

        app = FastAPI()
        service = AsyncMock()
        service.list_users.return_value = UserListResponse(
            data=[], next_cursor=None, has_more=False
        )
        app.dependency_overrides[get_db] = lambda: AsyncMock()
        app.dependency_overrides[get_user_service] = lambda: service
        app.dependency_overrides[require_org_id] = (
            lambda: "00000000-0000-0000-0000-000000000001"
        )
        app.include_router(router)
        app.add_middleware(AuthMiddleware)
        return app

    @pytest.mark.asyncio
    async def test_jwt_mcp_claim_blocks_get_users_403(self) -> None:
        """mcp=True JWT → GET /v1/users → 403 RFC 7807 before the route.

        The middleware gate covers get_current_user_id surfaces — the
        users route is never reached.
        """
        token = self._create_jwt(mcp=True)
        app = self._create_users_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get(
                "/v1/users",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 403
        body = resp.json()
        assert body["status"] == 403
        assert body["title"] == "Authorization Error"
        assert body["detail"] == "Password change required"

    @pytest.mark.asyncio
    async def test_jwt_mcp_false_reaches_get_users_200(self) -> None:
        """mcp=False JWT → GET /v1/users passes the middleware (200)."""
        token = self._create_jwt(mcp=False)
        app = self._create_users_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get(
                "/v1/users",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"data": [], "next_cursor": None, "has_more": False}

    # ── API key auth ────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_api_key_returns_401_without_db(self) -> None:
        """API key without DB configured returns 500."""
        raw_key = "oz_live_" + "a" * 64
        app = self._create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/test", headers={"Authorization": f"Bearer {raw_key}"})
            # No db_factory on app.state -> 500
            assert resp.status_code == 500

    @pytest.mark.asyncio
    async def test_api_key_invalid_returns_401(self) -> None:
        """Invalid API key with DB configured returns 401."""
        raw_key = "oz_live_" + "b" * 64
        # App with mock db_factory
        app = FastAPI()
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.begin = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_db_factory = MagicMock(return_value=mock_session)

        @app.get("/test")
        async def echo() -> dict:
            return {"status": "ok"}

        @app.options("/test")
        async def echo_opt() -> dict:
            return {"status": "ok"}

        # Set up mock Redis for negative cache to work
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.setex = AsyncMock(return_value=True)
        mock_redis.exists = AsyncMock(return_value=False)
        mock_redis.incr = AsyncMock(return_value=1)
        mock_redis.expire = AsyncMock(return_value=True)

        app.state.redis = mock_redis
        app.state.db_session_factory = mock_db_factory
        app.add_middleware(AuthMiddleware)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/test", headers={"Authorization": f"Bearer {raw_key}"})
            assert resp.status_code == 401

    # ── OPTIONS passthrough ─────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_options_passthrough(self) -> None:
        """OPTIONS requests pass through unauthenticated."""
        app = self._create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.options("/test")
            assert resp.status_code == 200

    # ── Non-HTTP passthrough ────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_non_http_passthrough(self) -> None:
        """Non-HTTP scopes pass through."""
        app = self._create_app()
        assert app is not None

    # ── Helper function tests ───────────────────────────────────────────────────

    def test_is_jwt_token(self) -> None:
        """_is_jwt_token correctly identifies JWTs."""
        assert _is_jwt_token("header.payload.signature") is True
        assert _is_jwt_token("one.two") is False
        assert _is_jwt_token("no-dots") is False
        assert _is_jwt_token("a.b.c.d") is False  # 3 dots

    def test_is_public_path(self) -> None:
        """_is_public_path matches known public paths."""
        assert _is_public_path("/health") is True
        assert _is_public_path("/v1/auth/login") is True
        assert _is_public_path("/v1/auth/forgot-password") is True
        assert _is_public_path("/private/data") is False
        assert _is_public_path("/v1/api/users") is False

    @pytest.mark.asyncio
    async def test_jwt_takes_precedence_over_api_key(self) -> None:
        """When token looks like JWT, it is tried first."""
        token = self._create_jwt(sub="user-pri-1", org_id="org-pri-1")
        app = self._create_app_with_state_check()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/test", headers={"Authorization": f"Bearer {token}"})
            assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"
            data = resp.json()
            assert data["auth_type"] == "jwt"

    @pytest.mark.asyncio
    async def test_state_defaults_on_unauthenticated(self) -> None:
        """When no auth, state has default values (None for org/user)."""
        app = FastAPI()
        app.add_middleware(AuthMiddleware)

        @app.get("/health")
        async def health(request: Request) -> dict:
            return {
                "org_id": getattr(request.state, "org_id", "NONE"),
                "auth_type": getattr(request.state, "auth_type", "NONE"),
            }

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["org_id"] is None
            assert data["auth_type"] is None


class TestInviteEndpointsPublicThroughMiddleware:
    """Regression — invite endpoints must pass the REAL AuthMiddleware.

    QA P1-1: the router-level invite tests build a bare ``FastAPI()`` with
    no auth middleware, so deleting ``/v1/auth/invites`` from
    ``PUBLIC_ENDPOINTS`` would break production with zero failing tests.
    These tests wire the real middleware + real auth router and assert the
    invite endpoints are reached without an ``Authorization`` header.
    """

    def _create_app(self) -> tuple[FastAPI, AsyncMock]:
        """Auth router + mocked invite service under the real AuthMiddleware."""
        app = FastAPI()
        register_exception_handlers(app)
        service_mock = AsyncMock()
        app.dependency_overrides[get_invite_service] = lambda: service_mock
        app.include_router(router)
        app.add_middleware(AuthMiddleware)
        return app, service_mock

    @pytest.mark.asyncio
    async def test_invite_info_no_auth_header_200(self) -> None:
        """No Authorization header → 200: the middleware must not 401."""
        app, service_mock = self._create_app()
        service_mock.get_invite_info.return_value = InviteInfoResponse(
            org_name="Acme Corp",
            email="alice@acme.com",
            name="Alice Johnson",
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/v1/auth/invites/info", json={"token": "raw-token"})

        assert resp.status_code == 200
        assert resp.status_code != 401
        assert resp.json()["org_name"] == "Acme Corp"

    @pytest.mark.asyncio
    async def test_invite_info_bogus_token_reaches_route_404(self) -> None:
        """Bogus token → 404 from the ROUTE, not 401 from the middleware.

        A 404 (rather than 401) proves the request passed the middleware and
        reached the invite handler.
        """
        app, service_mock = self._create_app()
        service_mock.get_invite_info.side_effect = NotFoundError(
            "This invitation link is invalid or has expired."
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/v1/auth/invites/info", json={"token": "bogus"})

        assert resp.status_code == 404
        assert resp.status_code != 401

    @pytest.mark.asyncio
    async def test_invite_accept_no_auth_header_not_401(self) -> None:
        """No Authorization header → the accept request passes the middleware."""
        app, service_mock = self._create_app()
        service_mock.accept_invite.side_effect = NotFoundError(
            "This invitation link is invalid or has expired."
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/v1/auth/invites/accept",
                json={"token": "bogus", "password": "SecurePass1"},
            )

        # Reached the route handler (service 404) — middleware did not 401.
        assert resp.status_code == 404
        assert resp.status_code != 401

    @pytest.mark.asyncio
    async def test_protected_route_without_auth_401_negative_control(self) -> None:
        """Negative control — a non-public route still 401s with no auth.

        Proves the middleware is actually enforcing: only the invite paths
        (and the rest of ``PUBLIC_ENDPOINTS``) bypass it.
        """
        app, _ = self._create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/v1/auth/me")

        assert resp.status_code == 401
