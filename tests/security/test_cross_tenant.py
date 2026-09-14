"""Cross-tenant isolation tests on the REAL API contract.

Verifies that one organization cannot read, list, or manipulate another
organization's data through any API endpoint.  Every test drives the real
app (``isolated_app``: testcontainers Postgres + Redis, Alembic-migrated)
with real cross-org UUIDs minted by ``bootstrap_tenant`` — no hardcoded
string IDs, no ``page``/``per_page`` params, no ``/v1/sessions`` prefix.

Gate truth table (verified against the routers/services, not guessed):

  ┌──────────────────────────────────────────────┬────────┬───────────────┐
  │ Operation (org A credential, org B resource) │ Status │ Deciding gate │
  ├──────────────────────────────────────────────┼────────┼───────────────┤
  │ GET /v1/users/<B user UUID>                  │ 404    │ service org-  │
  │ PATCH /v1/users/<B user UUID>                │ 404    │ scope →       │
  │ DELETE /v1/users/<B user UUID>               │ 404    │ NotFoundError │
  │ GET /v1/users (list, cursor pagination)      │ 200    │ service org-  │
  │                                              │        │ scope (own    │
  │                                              │        │ rows only)    │
  │ GET/POST/DELETE                              │ 403    │ require_      │
  │ /v1/projects/<B project>/sessions...         │        │ project_      │
  │                                              │        │ membership    │
  │                                              │        │ (API-key      │
  │                                              │        │ scope         │
  │                                              │        │ mismatch)     │
  │ GET /v1/projects/<B project>/search?...      │ 403    │ same as above │
  └──────────────────────────────────────────────┴────────┴───────────────┘

Global ``GET /v1/search`` is excluded (see test 8 docstring): the service
fans three queries out on one ``AsyncSession`` and 500s — filed for
``@senior-backend-dev``.

Why 404 (not 403) for users: the bootstrap API key carries
``members:read``/``members:write``, so ``require_permission[_or_self]``
passes and the org-scoped service lookup misses → ``NotFoundError`` → 404
(``core/exceptions.py`` mapping).

Why 403 (not 404) for project-scoped URLs: ``require_project_membership``
rejects an API key scoped to a different project before any service code
runs — and it never discloses whether the foreign project exists.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest

if TYPE_CHECKING:
    from httpx import AsyncClient

pytestmark = pytest.mark.security


# ── Seed helpers (real contract) ──────────────────────────────────────────


async def _create_user(client: AsyncClient, external_id: str) -> dict[str, Any]:
    """Create a user via ``POST /v1/users``; return the response body."""
    resp = await client.post("/v1/users", json={"external_id": external_id})
    assert resp.status_code == 201, f"User creation failed: {resp.text}"
    body = resp.json()
    UUID(body["id"])  # fail fast if the contract ever stops returning a UUID
    return body


async def _create_session(
    client: AsyncClient, project_id: UUID, external_id: str
) -> dict[str, Any]:
    """Create a session via ``POST /v1/projects/{id}/sessions``."""
    resp = await client.post(
        f"/v1/projects/{project_id}/sessions",
        json={"external_id": external_id, "metadata": {"seed": "x-tenant"}},
    )
    assert resp.status_code == 201, f"Session creation failed: {resp.text}"
    body = resp.json()
    UUID(body["id"])
    return body


# ── Search fixtures (mirror tests/integration/test_search_facts.py) ───────
# ``isolated_app`` does not run the lifespan, so the graph-backend
# dispatcher must be wired manually; the vector leg needs a stubbed
# embedding backend (zero vectors match the ``vector(1536)`` column).


@dataclass
class _FakeEmbedResponse:
    embeddings: list[list[float]] | None = None


class _FakeEmbedBackend:
    async def embed(
        self, texts: list[str], model: str | None = None
    ) -> _FakeEmbedResponse:
        return _FakeEmbedResponse(embeddings=[[0.0] * 1536 for _ in texts])


async def _fake_resolve_backend(
    provider: Any = None, org_config: Any = None, mode: Any = None
) -> _FakeEmbedBackend:
    return _FakeEmbedBackend()


@pytest.fixture()
def _search_dispatcher(isolated_app: Any) -> None:
    from core.graph_backend import init_dispatcher

    isolated_app.state.graph_backend_dispatcher = init_dispatcher()


@pytest.fixture()
def _fake_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.llm.resolve_backend", _fake_resolve_backend)


# ── Tests ─────────────────────────────────────────────────────────────────


class TestCrossTenantIsolation:
    """Org A cannot access org B/C data via any endpoint."""

    # ── User isolation ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    @pytest.mark.parametrize("target", ["b", "c"])
    async def test_cross_org_get_user_returns_404(
        self,
        auth_client_org_a: AsyncClient,
        auth_client_org_b: AsyncClient,
        auth_client_org_c: AsyncClient,
        target: str,
    ) -> None:
        """GET /v1/users/<foreign UUID> → 404 (org-scoped service lookup)."""
        # Seed via the owning org's client so the row genuinely exists there.
        seed_clients = {"b": auth_client_org_b, "c": auth_client_org_c}
        seeded = await _create_user(seed_clients[target], f"xtenant-user-{target}")
        foreign_id = seeded["id"]

        resp = await auth_client_org_a.get(f"/v1/users/{foreign_id}")
        assert resp.status_code == 404, (
            f"GET /v1/users/{foreign_id} returned {resp.status_code}, expected 404"
        )

    @pytest.mark.asyncio
    async def test_list_users_returns_only_own_org(
        self,
        auth_client_org_a: AsyncClient,
        auth_client_org_b: AsyncClient,
    ) -> None:
        """GET /v1/users returns only the caller's org members (non-vacuous)."""
        await _create_user(auth_client_org_a, "xtenant-a-alice")
        await _create_user(auth_client_org_a, "xtenant-a-bob")
        await _create_user(auth_client_org_b, "xtenant-b-carol")
        await _create_user(auth_client_org_b, "xtenant-b-dave")

        resp_a = await auth_client_org_a.get("/v1/users", params={"limit": 200})
        resp_b = await auth_client_org_b.get("/v1/users", params={"limit": 200})
        assert resp_a.status_code == 200
        assert resp_b.status_code == 200

        data_a = resp_a.json()["data"]
        data_b = resp_b.json()["data"]
        ext_a = {u["external_id"] for u in data_a}
        ext_b = {u["external_id"] for u in data_b}
        # Guard against vacuous disjointness: both sides must hold seeded rows.
        assert {"xtenant-a-alice", "xtenant-a-bob"} <= ext_a
        assert {"xtenant-b-carol", "xtenant-b-dave"} <= ext_b

        ids_a = {u["id"] for u in data_a}
        ids_b = {u["id"] for u in data_b}
        assert ids_a.isdisjoint(ids_b), (
            f"Overlapping user IDs between orgs: {ids_a & ids_b}"
        )
        org_ids_a = {u["organization_id"] for u in data_a}
        org_ids_b = {u["organization_id"] for u in data_b}
        assert len(org_ids_a) == 1 and len(org_ids_b) == 1
        assert org_ids_a != org_ids_b

    @pytest.mark.asyncio
    async def test_cross_org_update_and_delete_user_return_404(
        self,
        auth_client_org_a: AsyncClient,
        auth_client_org_b: AsyncClient,
    ) -> None:
        """PATCH/DELETE /v1/users/<foreign UUID> → 404; row untouched."""
        seeded = await _create_user(auth_client_org_b, "xtenant-b-erin")
        foreign_id = seeded["id"]

        patch_resp = await auth_client_org_a.patch(
            f"/v1/users/{foreign_id}", json={"name": "Mallory"}
        )
        assert patch_resp.status_code == 404, (
            f"PATCH returned {patch_resp.status_code}, expected 404"
        )

        delete_resp = await auth_client_org_a.delete(f"/v1/users/{foreign_id}")
        assert delete_resp.status_code == 404, (
            f"DELETE returned {delete_resp.status_code}, expected 404"
        )

        # No side effects: the owner still sees the unchanged row.
        check = await auth_client_org_b.get(f"/v1/users/{foreign_id}")
        assert check.status_code == 200
        assert check.json()["name"] != "Mallory"

    # ── Session isolation (project-scoped URLs) ─────────────────────────

    @pytest.mark.asyncio
    @pytest.mark.parametrize("target", ["b", "c"])
    async def test_cross_org_get_session_returns_403(
        self,
        auth_client_org_a: AsyncClient,
        auth_client_org_b: AsyncClient,
        auth_client_org_c: AsyncClient,
        tenants: dict[str, dict[str, Any]],
        target: str,
    ) -> None:
        """GET on a foreign project's session URL → 403 (membership gate)."""
        clients = {
            "b": auth_client_org_b,
            "c": auth_client_org_c,
        }
        seeded = await _create_session(
            clients[target], tenants[target]["project_id"], f"xtenant-sess-{target}"
        )

        resp = await auth_client_org_a.get(
            f"/v1/projects/{tenants[target]['project_id']}/sessions/{seeded['id']}"
        )
        assert resp.status_code == 403, (
            f"Cross-project GET returned {resp.status_code}, expected 403"
        )

    @pytest.mark.asyncio
    async def test_cross_org_create_session_returns_403(
        self,
        auth_client_org_a: AsyncClient,
        auth_client_org_b: AsyncClient,
        tenants: dict[str, dict[str, Any]],
    ) -> None:
        """POST to a foreign project's sessions URL → 403 with no side effect."""
        resp = await auth_client_org_a.post(
            f"/v1/projects/{tenants['b']['project_id']}/sessions",
            json={"external_id": "xtenant-evil", "metadata": {}},
        )
        assert resp.status_code == 403, (
            f"Cross-project POST returned {resp.status_code}, expected 403"
        )

        listing = await auth_client_org_b.get(
            f"/v1/projects/{tenants['b']['project_id']}/sessions"
        )
        assert listing.status_code == 200
        assert "xtenant-evil" not in {s["external_id"] for s in listing.json()["data"]}

    @pytest.mark.asyncio
    async def test_cross_org_delete_session_returns_403(
        self,
        auth_client_org_a: AsyncClient,
        auth_client_org_b: AsyncClient,
        tenants: dict[str, dict[str, Any]],
    ) -> None:
        """DELETE on a foreign project's session URL → 403; row survives."""
        seeded = await _create_session(
            auth_client_org_b, tenants["b"]["project_id"], "xtenant-b-session"
        )

        resp = await auth_client_org_a.delete(
            f"/v1/projects/{tenants['b']['project_id']}/sessions/{seeded['id']}"
        )
        assert resp.status_code == 403, (
            f"Cross-project DELETE returned {resp.status_code}, expected 403"
        )

        check = await auth_client_org_b.get(
            f"/v1/projects/{tenants['b']['project_id']}/sessions/{seeded['id']}"
        )
        assert check.status_code == 200

    # ── Pagination does not leak ────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_paginated_list_does_not_leak(
        self,
        auth_client_org_a: AsyncClient,
        auth_client_org_b: AsyncClient,
    ) -> None:
        """Cursor-walking the whole A roster never yields a B row."""
        seeded_a = {
            (await _create_user(auth_client_org_a, f"xtenant-a-p{i}"))["id"]
            for i in range(3)
        }
        seeded_b = {
            (await _create_user(auth_client_org_b, f"xtenant-b-p{i}"))["id"]
            for i in range(2)
        }

        collected: set[str] = set()
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 1}
            if cursor is not None:
                params["cursor"] = cursor
            resp = await auth_client_org_a.get("/v1/users", params=params)
            assert resp.status_code == 200
            body = resp.json()
            collected.update(u["id"] for u in body["data"])
            cursor = body["next_cursor"]
            if not body["has_more"]:
                assert cursor is None
                break

        assert seeded_a <= collected, "Pagination missed own-org rows"
        assert collected.isdisjoint(seeded_b), "Paginated listing leaked!"

    # ── Search leakage (project hybrid search) ──────────────────────────

    @pytest.mark.asyncio
    async def test_cross_tenant_search_does_not_leak(
        self,
        auth_client_org_a: AsyncClient,
        auth_client_org_b: AsyncClient,
        tenants: dict[str, dict[str, Any]],
        _search_dispatcher: None,
        _fake_embeddings: None,
    ) -> None:
        """Seeded fact/episode in org B is invisible to org A search.

        Exercises the real hybrid endpoint
        ``GET /v1/projects/{project_id}/search`` (deterministic BM25 leg):

        - Positive control first (org B finds its own rows — guards
          vacuous emptiness), then org A's own-project search returns
          zero org-B rows, and the cross-project URL is gated 403.

        .. note::
            Global ``GET /v1/search`` is deliberately NOT covered here:
            ``GlobalSearchService.search`` fans three queries out via
            ``asyncio.gather`` on a single ``AsyncSession``
            (``services/global_search_service.py:48``), which raises
            ``InvalidRequestError: ... concurrent operations are not
            permitted`` (→ HTTP 500).  That is a production race filed
            for ``@senior-backend-dev`` — add a global-search leakage
            test once it is fixed.
        """
        marker = "quuxnimbus"
        session_ext = f"xtenant-{marker}-session"
        project_b = tenants["b"]["project_id"]
        project_a = tenants["a"]["project_id"]

        sess = await _create_session(auth_client_org_b, project_b, session_ext)

        fact_resp = await auth_client_org_b.post(
            f"/v1/projects/{project_b}/facts",
            json={
                "session_id": session_ext,
                "facts": [
                    {
                        "subject": "Zyqx",
                        "predicate": "loves",
                        "object": f"mountain {marker} hiking",
                    }
                ],
            },
        )
        assert fact_resp.status_code == 202, f"Fact ingest failed: {fact_resp.text}"

        episode_resp = await auth_client_org_b.post(
            f"/v1/projects/{project_b}/memory",
            data={
                "data": json.dumps(
                    {
                        "session_id": session_ext,
                        "messages": [
                            {
                                "role": "user",
                                "content": f"I love {marker} trails in Colorado",
                            }
                        ],
                    }
                )
            },
        )
        assert episode_resp.status_code == 202, (
            f"Episode ingest failed: {episode_resp.text}"
        )

        # ── Positive control: org B finds its own rows ──────────────────
        control = await auth_client_org_b.get(
            f"/v1/projects/{project_b}/search",
            params={"query": marker, "types": "facts"},
        )
        assert control.status_code == 200, f"Control search failed: {control.text}"
        control_hits = [
            r for r in control.json()["results"] if marker in r.get("content", "")
        ]
        assert control_hits, "Control search found nothing — leakage asserts vacuous"

        # ── Org A's own-project search: 200, zero org-B rows ────────────
        own = await auth_client_org_a.get(
            f"/v1/projects/{project_a}/search",
            params={"query": marker, "types": "episodes,facts"},
        )
        assert own.status_code == 200
        leaked = [
            r
            for r in own.json()["results"]
            if marker in r.get("content", "") or r.get("id") == sess["id"]
        ]
        assert leaked == [], f"Project search leaked cross-org rows: {leaked}"

        # ── Cross-project search URL: gated 403 ─────────────────────────
        gated = await auth_client_org_a.get(
            f"/v1/projects/{project_b}/search", params={"query": marker}
        )
        assert gated.status_code == 403, (
            f"Cross-project search returned {gated.status_code}, expected 403"
        )
