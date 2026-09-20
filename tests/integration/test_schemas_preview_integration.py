"""Integration tests for the new schema builder endpoints.

Endpoints under test:

    GET    /v1/admin/schemas/templates — List static starter templates
    POST   /v1/admin/schemas/preview   — Preview extraction (no persistence)
    POST   /v1/admin/schemas           — Create schema (CRUD contract)
    GET    /v1/admin/schemas           — List schemas with type filter
    GET    /v1/admin/schemas/{id}      — Get single schema
    PUT    /v1/admin/schemas/{id}      — Update schema
    DELETE /v1/admin/schemas/{id}      — Soft-delete schema

Live smoke truth encoded here:

    - templates → 200, exactly 6 starters, bare-array wire shape
    - preview happy → 200, ``data == {"total": 42}``, zero DB writes
    - preview invalid-schema → 422 with the dotted field path
    - preview unparseable output → 200, ``data == {}`` + loud errors
    - CRUD → 201 echo, duplicate 409, soft-delete 204 (``is_active=false``)
    - no token → 401 on preview + templates

Test strategy:

    Real Postgres 15 + Redis via testcontainers (``isolated_app``), real
    HTTP via ``httpx.AsyncClient`` + ``ASGITransport``, real auth
    middleware with org-scoped admin JWTs.  The only stub is the LLM
    backend at the service boundary (``core.llm.resolve_backend``) — the
    same convention as the other integration suites — because CI has no
    live LLM provider and tests must never hit real LLM APIs.  Created
    schemas need no manual cleanup: per-test table truncation wipes them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import AsyncClient

from tests.integration.conftest import asgi_transport

TEMPLATE_KEYS = {"invoice", "contact", "order", "meeting_notes", "feedback", "receipt"}

HAPPY_SCHEMA = {
    "type": "object",
    "properties": {"total": {"type": "number"}},
    "required": ["total"],
}


@pytest_asyncio.fixture(loop_scope="function")
async def admin_client(
    isolated_app: Any,
    isolated_org_and_key: dict,
) -> AsyncGenerator[AsyncClient, None]:
    """JWT-authenticated client — schema admin is a dashboard (JWT) operation."""
    transport = asgi_transport(isolated_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {isolated_org_and_key['jwt']}"
        yield client


@pytest_asyncio.fixture(loop_scope="function")
async def anon_client(isolated_app: Any) -> AsyncGenerator[AsyncClient, None]:
    """Unauthenticated client — for the 401 gating tests."""
    transport = asgi_transport(isolated_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


class _FakeChatResponse:
    """Minimal chat response — the service only reads ``.content``."""

    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChatBackend:
    """Deterministic LLM backend — returns canned content, no network."""

    def __init__(self, content: str) -> None:
        self._content = content

    async def chat(self, messages: list[dict], **kwargs: Any) -> _FakeChatResponse:
        return _FakeChatResponse(self._content)


@pytest.fixture
def stub_llm(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Stub ``core.llm.resolve_backend`` with canned output.

    Returns a mutable holder so each test controls the LLM text.  Default
    is the smoke happy-path payload (``{"total": 42}``).
    """
    state = {"content": '{"total": 42}'}

    async def _fake_resolve_backend(
        provider: str | None = None,
        org_config: dict | None = None,
        mode: str | None = None,
    ) -> _FakeChatBackend:
        return _FakeChatBackend(state["content"])

    monkeypatch.setattr("core.llm.resolve_backend", _fake_resolve_backend)
    return state


_COUNT_QUERIES = {
    "extraction_schemas": "SELECT COUNT(*) FROM extraction_schemas",
    "structured_extractions": "SELECT COUNT(*) FROM structured_extractions",
}


async def _table_count(app: Any, table: str) -> int:
    """Return the row count for a known table (allowlisted — no SQL injection)."""
    from sqlalchemy import text as _sql

    query = _COUNT_QUERIES[table]
    async with app.state.db_session_factory() as session:
        result = await session.execute(_sql(query))
        return int(result.scalar_one())


def _unique_name(prefix: str) -> str:
    """Unique schema name per test — no cross-test collisions."""
    return f"{prefix}_{uuid4().hex[:8]}"


class TestSchemaTemplates:
    """GET /v1/admin/schemas/templates — static starter catalogue."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_templates_returns_six_starters(
        self, admin_client: AsyncClient
    ) -> None:
        """200, bare array of 6, expected keys, schema + sample per entry."""
        response = await admin_client.get("/v1/admin/schemas/templates")
        assert response.status_code == 200, f"Expected 200: {response.text}"
        body = response.json()
        assert isinstance(body, list), f"Wire shape is a bare array: {body!r:.200}"
        assert len(body) == 6, f"Expected 6 templates, got {len(body)}"
        assert {t["key"] for t in body} == TEMPLATE_KEYS
        for template in body:
            assert template["json_schema"].get("properties"), (
                f"Template {template['key']} missing json_schema.properties"
            )
            assert template["sample_text"], (
                f"Template {template['key']} missing sample_text"
            )


class TestSchemaPreview:
    """POST /v1/admin/schemas/preview — no-persist extraction dry-run."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_preview_happy_path_writes_zero_rows(
        self, isolated_app: Any, admin_client: AsyncClient, stub_llm: dict
    ) -> None:
        """200 + data + no errors, and zero rows in both tables."""
        assert stub_llm["content"] == '{"total": 42}'
        before_schemas = await _table_count(isolated_app, "extraction_schemas")
        before_extractions = await _table_count(isolated_app, "structured_extractions")

        response = await admin_client.post(
            "/v1/admin/schemas/preview",
            json={"json_schema": HAPPY_SCHEMA, "sample_text": "Order total $42.00."},
        )

        assert response.status_code == 200, f"Expected 200: {response.text}"
        body = response.json()
        assert body["data"] == {"total": 42}
        assert body["validation_errors"] == []
        assert await _table_count(isolated_app, "extraction_schemas") == before_schemas
        assert (
            await _table_count(isolated_app, "structured_extractions")
            == before_extractions
        )

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_preview_invalid_schema_returns_path_qualified_422(
        self, admin_client: AsyncClient
    ) -> None:
        """Bogus property type → 422 naming ``properties.x.type``."""
        response = await admin_client.post(
            "/v1/admin/schemas/preview",
            json={
                "json_schema": {
                    "type": "object",
                    "properties": {"x": {"type": "bogus"}},
                },
                "sample_text": "Some sample text.",
            },
        )
        assert response.status_code == 422, f"Expected 422: {response.text}"
        assert "properties.x.type" in response.text

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_preview_unparseable_output_is_loud(
        self, admin_client: AsyncClient, stub_llm: dict
    ) -> None:
        """Garbage LLM output → 200 with empty data + non-empty errors."""
        stub_llm["content"] = "not json at all, no braces here"
        response = await admin_client.post(
            "/v1/admin/schemas/preview",
            json={"json_schema": HAPPY_SCHEMA, "sample_text": "Order total $42.00."},
        )
        assert response.status_code == 200, f"Expected 200: {response.text}"
        body = response.json()
        assert body["data"] == {}
        assert body["validation_errors"], "Unparseable output must surface errors"


class TestSchemaCrudContract:
    """CRUD lifecycle for the smoke-covered schema endpoints."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_crud_lifecycle_create_get_update_soft_delete(
        self, admin_client: AsyncClient
    ) -> None:
        """201 echo → 200 get → 200 rename → 204 → get shows inactive."""
        name = _unique_name("crud_schema")
        schema = {
            "type": "object",
            "properties": {"total": {"type": "number"}},
            "required": ["total"],
        }

        created = await admin_client.post(
            "/v1/admin/schemas",
            json={"name": name, "type": "structured", "json_schema": schema},
        )
        assert created.status_code == 201, f"Expected 201: {created.text}"
        created_body = created.json()
        assert created_body["name"] == name
        assert created_body["type"] == "structured"
        assert created_body["json_schema"] == schema
        assert created_body["is_active"] is True
        schema_id = created_body["id"]

        got = await admin_client.get(f"/v1/admin/schemas/{schema_id}")
        assert got.status_code == 200, f"Expected 200: {got.text}"
        assert got.json()["id"] == schema_id

        renamed = _unique_name("crud_renamed")
        updated = await admin_client.put(
            f"/v1/admin/schemas/{schema_id}", json={"name": renamed}
        )
        assert updated.status_code == 200, f"Expected 200: {updated.text}"
        assert updated.json()["name"] == renamed

        deleted = await admin_client.delete(f"/v1/admin/schemas/{schema_id}")
        assert deleted.status_code == 204, f"Expected 204: {deleted.text}"

        after = await admin_client.get(f"/v1/admin/schemas/{schema_id}")
        assert after.status_code == 200, f"Expected 200: {after.text}"
        assert after.json()["is_active"] is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_duplicate_name_returns_409(
        self, admin_client: AsyncClient
    ) -> None:
        """Second create with the same name → 409."""
        name = _unique_name("dup_schema")
        payload = {
            "name": name,
            "type": "structured",
            "json_schema": {"type": "object"},
        }
        first = await admin_client.post("/v1/admin/schemas", json=payload)
        assert first.status_code == 201, f"Setup failed: {first.text}"
        second = await admin_client.post("/v1/admin/schemas", json=payload)
        assert second.status_code == 409, f"Expected 409: {second.text}"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_filter_type_structured(self, admin_client: AsyncClient) -> None:
        """GET ?type=structured returns only structured schemas."""
        structured_name = _unique_name("filter_structured")
        classification_name = _unique_name("filter_class")
        for payload in (
            {
                "name": structured_name,
                "type": "structured",
                "json_schema": {"type": "object"},
            },
            {
                "name": classification_name,
                "type": "classification",
                "json_schema": {"intent": ["hello"]},
            },
        ):
            resp = await admin_client.post("/v1/admin/schemas", json=payload)
            assert resp.status_code == 201, f"Setup failed: {resp.text}"

        response = await admin_client.get(
            "/v1/admin/schemas", params={"type": "structured"}
        )
        assert response.status_code == 200, f"Expected 200: {response.text}"
        body = response.json()
        assert body["total"] >= 1
        names = {s["name"] for s in body["data"]}
        assert all(s["type"] == "structured" for s in body["data"])
        assert structured_name in names
        assert classification_name not in names


class TestSchemaAuthGating:
    """Unauthenticated requests → 401 on preview + templates."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_templates_requires_auth(self, anon_client: AsyncClient) -> None:
        """GET templates with no token → 401."""
        response = await anon_client.get("/v1/admin/schemas/templates")
        assert response.status_code == 401, f"Expected 401: {response.text}"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_preview_requires_auth(self, anon_client: AsyncClient) -> None:
        """POST preview with no token → 401."""
        response = await anon_client.post(
            "/v1/admin/schemas/preview",
            json={"json_schema": HAPPY_SCHEMA, "sample_text": "Order total $42.00."},
        )
        assert response.status_code == 401, f"Expected 401: {response.text}"
