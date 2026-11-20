"""Endpoint contract for ``GET /v1/admin/llm-usage``.

GROUP 3: 401 unauthenticated, 403 without ``members:read``, 422 on
over-limit params and bad ``sort_by``; worker/model/project/date-range
filters reach the service; the summary equals the rows' sums;
``sort_by=total_tokens`` is accepted (422 regression guard).

Service-layer functions are mocked — no DB, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.db import get_db
from repositories.llm_usage_repository import UsageSummary
from routers.admin_llm_usage import router

pytestmark = pytest.mark.unit

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")


def _row(**overrides: object) -> SimpleNamespace:
    prompt = int(overrides.get("prompt_tokens", 10))  # type: ignore[arg-type]
    completion = int(overrides.get("completion_tokens", 5))  # type: ignore[arg-type]
    return SimpleNamespace(
        id=overrides.get("id", uuid4()),
        user_id=overrides.get("user_id", ORG_ID),
        provider=overrides.get("provider", "openai"),
        model=overrides.get("model", "gpt-4o-mini"),
        worker=overrides.get("worker", "enrich_episode"),
        project_id=overrides.get("project_id"),
        episode_id=overrides.get("episode_id"),
        session_id=overrides.get("session_id"),
        community_id=overrides.get("community_id"),
        task_run_id=overrides.get("task_run_id"),
        prompt_tokens=prompt,
        completion_tokens=completion,
        reasoning_tokens=overrides.get("reasoning_tokens", 0),
        cache_read_input_tokens=overrides.get("cache_read_input_tokens", 0),
        cache_creation_input_tokens=overrides.get("cache_creation_input_tokens", 0),
        total_tokens=overrides.get("total_tokens", prompt + completion),
        embed_count=overrides.get("embed_count"),
        embed_dim=overrides.get("embed_dim"),
        duration_ms=overrides.get("duration_ms", 100),
        created_at=overrides.get("created_at", datetime.now(UTC)),
    )


def _summary_for(rows: list[SimpleNamespace]) -> UsageSummary:
    durations = [r.duration_ms for r in rows]
    return UsageSummary(
        calls=len(rows),
        prompt_tokens=sum(r.prompt_tokens for r in rows),
        completion_tokens=sum(r.completion_tokens for r in rows),
        reasoning_tokens=sum(r.reasoning_tokens for r in rows),
        total_tokens=sum(r.total_tokens for r in rows),
        avg_duration_ms=sum(durations) / len(durations) if durations else 0.0,
    )


def _make_app(*, authenticated: bool) -> FastAPI:
    app = FastAPI()
    app.dependency_overrides[get_db] = lambda: AsyncMock(spec=AsyncSession)
    if authenticated:

        @app.middleware("http")
        async def _auth(request, call_next):  # type: ignore[no-untyped-def]
            request.state.user_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            return await call_next(request)

        app.state.redis = AsyncMock()
    app.include_router(router)
    return app


def _mock_service(
    rows: list[SimpleNamespace] | None = None,
) -> tuple[AsyncMock, AsyncMock]:
    rows = rows if rows is not None else [_row()]
    list_mock = AsyncMock(return_value=(rows, len(rows)))
    summary_mock = AsyncMock(return_value=_summary_for(rows))
    return list_mock, summary_mock


class TestAuth:
    async def test_unauthenticated_returns_401(self) -> None:
        app = _make_app(authenticated=False)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/v1/admin/llm-usage")
        assert resp.status_code == 401

    async def test_member_without_permission_returns_403(self) -> None:
        app = _make_app(authenticated=True)
        with (
            patch(
                "dependencies.auth.get_user_role",
                new=AsyncMock(return_value="member"),
            ),
            patch(
                "dependencies.auth.get_effective_permissions",
                new=AsyncMock(return_value=frozenset()),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/v1/admin/llm-usage")
        assert resp.status_code == 403


class TestValidation:
    async def test_days_over_limit_returns_422(self) -> None:
        app = _make_app(authenticated=True)
        list_mock, summary_mock = _mock_service([])
        with (
            patch("dependencies.auth._check_permission", new=AsyncMock()),
            patch("routers.admin_llm_usage.list_usage", list_mock),
            patch("routers.admin_llm_usage.summarize_usage", summary_mock),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/v1/admin/llm-usage", params={"days": 999})
        assert resp.status_code == 422

    async def test_limit_over_limit_returns_422(self) -> None:
        app = _make_app(authenticated=True)
        list_mock, summary_mock = _mock_service([])
        with (
            patch("dependencies.auth._check_permission", new=AsyncMock()),
            patch("routers.admin_llm_usage.list_usage", list_mock),
            patch("routers.admin_llm_usage.summarize_usage", summary_mock),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/v1/admin/llm-usage", params={"limit": 501})
        assert resp.status_code == 422

    async def test_bad_sort_by_returns_422(self) -> None:
        app = _make_app(authenticated=True)
        list_mock, summary_mock = _mock_service([])
        with (
            patch("dependencies.auth._check_permission", new=AsyncMock()),
            patch("routers.admin_llm_usage.list_usage", list_mock),
            patch("routers.admin_llm_usage.summarize_usage", summary_mock),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(
                    "/v1/admin/llm-usage", params={"sort_by": "bogus"}
                )
        assert resp.status_code == 422

    async def test_from_without_to_returns_422(self) -> None:
        app = _make_app(authenticated=True)
        list_mock, summary_mock = _mock_service([])
        with (
            patch("dependencies.auth._check_permission", new=AsyncMock()),
            patch("routers.admin_llm_usage.list_usage", list_mock),
            patch("routers.admin_llm_usage.summarize_usage", summary_mock),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(
                    "/v1/admin/llm-usage", params={"from": "2026-01-01"}
                )
        assert resp.status_code == 422


class TestFilteringAndSummary:
    async def test_filters_reach_service(self) -> None:
        app = _make_app(authenticated=True)
        rows = [_row(worker="enrich_episode", model="gpt-4o-mini")]
        list_mock, summary_mock = _mock_service(rows)
        with (
            patch("dependencies.auth._check_permission", new=AsyncMock()),
            patch("routers.admin_llm_usage.list_usage", list_mock),
            patch("routers.admin_llm_usage.summarize_usage", summary_mock),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(
                    "/v1/admin/llm-usage",
                    params={
                        "worker": "enrich_episode",
                        "model": "gpt-4o-mini",
                        "project_id": str(PROJECT_ID),
                        "from": "2026-01-01",
                        "to": "2026-02-01",
                    },
                )
        assert resp.status_code == 200
        _, kwargs = list_mock.call_args
        assert kwargs["worker"] == "enrich_episode"
        assert kwargs["model"] == "gpt-4o-mini"
        assert kwargs["project_id"] == PROJECT_ID
        assert kwargs["start"] is not None
        assert kwargs["end"] is not None
        _, summary_kwargs = summary_mock.call_args
        assert summary_kwargs["worker"] == "enrich_episode"
        assert summary_kwargs["model"] == "gpt-4o-mini"

    async def test_summary_totals_equal_row_sums(self) -> None:
        app = _make_app(authenticated=True)
        rows = [
            _row(prompt_tokens=10, completion_tokens=5, reasoning_tokens=2),
            _row(prompt_tokens=30, completion_tokens=15, reasoning_tokens=4),
        ]
        list_mock, summary_mock = _mock_service(rows)
        with (
            patch("dependencies.auth._check_permission", new=AsyncMock()),
            patch("routers.admin_llm_usage.list_usage", list_mock),
            patch("routers.admin_llm_usage.summarize_usage", summary_mock),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/v1/admin/llm-usage")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        summary = body["summary"]
        assert summary["calls"] == 2
        assert summary["prompt_tokens"] == 40
        assert summary["completion_tokens"] == 20
        assert summary["reasoning_tokens"] == 6
        assert summary["total_tokens"] == 60

    async def test_sort_by_total_tokens_accepted(self) -> None:
        app = _make_app(authenticated=True)
        list_mock, summary_mock = _mock_service([_row()])
        with (
            patch("dependencies.auth._check_permission", new=AsyncMock()),
            patch("routers.admin_llm_usage.list_usage", list_mock),
            patch("routers.admin_llm_usage.summarize_usage", summary_mock),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(
                    "/v1/admin/llm-usage", params={"sort_by": "total_tokens"}
                )
        assert resp.status_code == 200
        _, kwargs = list_mock.call_args
        assert kwargs["sort"].sort_by == "total_tokens"
