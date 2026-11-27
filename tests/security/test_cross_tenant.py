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
  │ GET /v1/users/<B user UUID> (member JWT)     │ 403    │ require_      │
  │ PATCH /v1/users/<B user UUID> (member JWT)   │ 403    │ admin_or_self │
  │ DELETE /v1/users/<B user UUID> (member JWT) │ 403    │ (platform     │
  │ GET /v1/users (member JWT)                   │ 403    │ admin only)   │
  │ GET /v1/users (superadmin)                   │ 200    │ platform-     │
  │                                              │        │ visible       │
  │ GET /v1/search?q=...                    │ 200    │ service org-  │
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

Global ``GET /v1/search`` is covered in test 8 (org-B project/user/session
rows seeded, zero leakage asserted with an org-A positive control).

Why 403 (not 404) for users: user endpoints are platform-admin-gated
(``require_admin`` / ``require_admin_or_self``) — a member JWT is neither
admin nor the target user, so the gate rejects before any service code
runs.  Members can only GET/PATCH their own record; listing and all
cross-user access need the platform admin (platform-visible, all rows).
User seeding therefore goes through the ``superadmin_client`` fixture —
project-scoped API keys cannot reach these endpoints at all (403 at the
permission gate).

Why 403 (not 404) for project-scoped URLs: ``require_project_membership``
rejects an API key scoped to a different project before any service code
runs — and it never discloses whether the foreign project exists.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest

from core.embeddings import CANONICAL_EMBED_DIM

if TYPE_CHECKING:
    from httpx import AsyncClient

pytestmark = pytest.mark.security


# ── Seed helpers (real contract) ──────────────────────────────────────────


async def _create_user(client: AsyncClient, external_id: str) -> dict[str, Any]:
    """Create a user via ``POST /v1/users``; return the response body.

    ``client`` must be the ``superadmin_client`` — user creation is
    platform-admin-gated (member JWTs get 403).
    """
    resp = await client.post("/v1/users", json={"external_id": external_id})
    assert resp.status_code == 201, f"User creation failed: {resp.text}"
    body = resp.json()
    UUID(body["id"])  # fail fast if the contract ever stops returning a UUID
    return body


async def _create_named_user(
    client: AsyncClient, external_id: str, name: str
) -> dict[str, Any]:
    """Create a user with a display name (needed for global-search labels).

    ``client`` must be the ``superadmin_client`` — see :func:`_create_user`.
    """
    resp = await client.post(
        "/v1/users", json={"external_id": external_id, "name": name}
    )
    assert resp.status_code == 201, f"User creation failed: {resp.text}"
    body = resp.json()
    UUID(body["id"])
    return body


async def _create_project_via_jwt(
    isolated_app: Any, jwt: str, name: str
) -> dict[str, Any]:
    """Create a project via ``POST /v1/projects`` with JWT auth.

    Project creation requires ``project:manage``, which the project-scoped
    API keys do not carry — so this drives the JWT path (as in
    ``bootstrap_tenant``) instead of the ``auth_client_*`` fixtures.
    """
    from httpx import AsyncClient

    from tests.integration.conftest import asgi_transport

    transport = asgi_transport(isolated_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {jwt}"
        resp = await client.post("/v1/projects", json={"name": name})
    assert resp.status_code == 201, f"Project creation failed: {resp.text}"
    body = resp.json()
    UUID(body["id"])
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
# embedder. The stub must return canonical-dim vectors — prod
# ``validate_embedding_dim`` (core/embeddings.py) hard-rejects any other
# length, so a stale literal here surfaces as a 503 ``search_leg_failed``.


async def _fake_embed(texts: list[str]) -> list[list[float]]:
    """Return one canonical-dim zero vector per text."""
    return [[0.0] * CANONICAL_EMBED_DIM for _ in texts]


def _patch_embedder(monkeypatch: Any) -> None:
    """Point both embedder entrypoints at ``_fake_embed``."""
    monkeypatch.setattr("core.embeddings.embed_query", _fake_embed)
    monkeypatch.setattr("core.embeddings.embed_passage", _fake_embed)


@pytest.fixture()
def _search_dispatcher(isolated_app: Any) -> None:
    from core.graph_backend import init_dispatcher

    isolated_app.state.graph_backend_dispatcher = init_dispatcher()


@pytest.fixture()
def _fake_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_embedder(monkeypatch)


# ── Tests ─────────────────────────────────────────────────────────────────


class TestCrossTenantIsolation:
    """Org A cannot access org B/C data via any endpoint."""

    # ── User isolation ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    @pytest.mark.parametrize("target", ["b", "c"])
    async def test_cross_org_get_user_returns_403(
        self,
        superadmin_client: AsyncClient,
        jwt_client_org_a: AsyncClient,
        target: str,
    ) -> None:
        """GET /v1/users/<foreign UUID> via member JWT → 403 (admin-only gate)."""
        # Seed via the platform admin (only it may create users).
        seed_map = {"b": "xtenant-user-b", "c": "xtenant-user-c"}
        seeded = await _create_user(superadmin_client, seed_map[target])
        foreign_id = seeded["id"]

        resp = await jwt_client_org_a.get(f"/v1/users/{foreign_id}")
        assert resp.status_code == 403, (
            f"GET /v1/users/{foreign_id} returned {resp.status_code}, expected 403"
        )

    @pytest.mark.asyncio
    async def test_list_users_member_forbidden_superadmin_visible(
        self,
        superadmin_client: AsyncClient,
        jwt_client_org_a: AsyncClient,
        jwt_client_org_b: AsyncClient,
    ) -> None:
        """User listing is platform-admin-only: members get 403.

        Seeds via the superadmin (platform-visible, all rows), asserts both
        member JWTs get 403 on ``GET /v1/users``, and guards vacuity by
        asserting the superadmin list contains every seeded row.
        """
        await _create_user(superadmin_client, "xtenant-a-alice")
        await _create_user(superadmin_client, "xtenant-a-bob")
        await _create_user(superadmin_client, "xtenant-b-carol")
        await _create_user(superadmin_client, "xtenant-b-dave")

        for member_client in (jwt_client_org_a, jwt_client_org_b):
            resp = await member_client.get("/v1/users", params={"limit": 200})
            assert resp.status_code == 403, (
                f"member user-list returned {resp.status_code}, expected 403"
            )

        admin_resp = await superadmin_client.get("/v1/users", params={"limit": 200})
        assert admin_resp.status_code == 200
        ext_all = {u["external_id"] for u in admin_resp.json()["data"]}
        assert {"xtenant-a-alice", "xtenant-a-bob"} <= ext_all
        assert {"xtenant-b-carol", "xtenant-b-dave"} <= ext_all

    @pytest.mark.asyncio
    async def test_cross_org_update_and_delete_user_return_403(
        self,
        superadmin_client: AsyncClient,
        jwt_client_org_a: AsyncClient,
    ) -> None:
        """PATCH/DELETE /v1/users/<foreign UUID> via member JWT → 403; row untouched."""
        seeded = await _create_user(superadmin_client, "xtenant-b-erin")
        foreign_id = seeded["id"]

        patch_resp = await jwt_client_org_a.patch(
            f"/v1/users/{foreign_id}", json={"name": "Mallory"}
        )
        assert patch_resp.status_code == 403, (
            f"PATCH returned {patch_resp.status_code}, expected 403"
        )

        delete_resp = await jwt_client_org_a.delete(f"/v1/users/{foreign_id}")
        assert delete_resp.status_code == 403, (
            f"DELETE returned {delete_resp.status_code}, expected 403"
        )

        # No side effects: the platform admin still sees the unchanged row.
        check = await superadmin_client.get(f"/v1/users/{foreign_id}")
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
        superadmin_client: AsyncClient,
        jwt_client_org_a: AsyncClient,
    ) -> None:
        """Member JWTs cannot paginate the roster at all — 403 leaks nothing."""
        seeded = {
            (await _create_user(superadmin_client, f"xtenant-p{i}"))["id"]
            for i in range(3)
        }

        # A member cursor-walk is rejected on the first page.
        resp = await jwt_client_org_a.get("/v1/users", params={"limit": 1})
        assert resp.status_code == 403, (
            f"member user-list returned {resp.status_code}, expected 403"
        )

        # Vacuity guard: the superadmin walk finds every seeded row.
        collected: set[str] = set()
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 1}
            if cursor is not None:
                params["cursor"] = cursor
            page = await superadmin_client.get("/v1/users", params=params)
            assert page.status_code == 200
            body = page.json()
            collected.update(u["id"] for u in body["data"])
            cursor = body["next_cursor"]
            if not body["has_more"]:
                assert cursor is None
                break

        assert seeded <= collected, "Superadmin walk missed seeded rows"

    # ── Search leakage (project hybrid search) ──────────────────────────

    @pytest.mark.asyncio
    async def test_cross_tenant_search_does_not_leak(
        self,
        isolated_app: Any,
        auth_client_org_a: AsyncClient,
        auth_client_org_b: AsyncClient,
        superadmin_client: AsyncClient,
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

        Also covers global ``GET /v1/search`` (``routers/global_search.py``,
        ``project:read``): org-B project/user/session rows matching the
        query must be absent from org A's results while org A's own
        matching user row is present (positive control).
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

        # ── Global search: org-B rows invisible to org A ────────────────
        # ``GET /v1/search`` runs three org-scoped ILIKE legs (projects,
        # users, sessions).  Seed one matching row per leg in org B plus
        # a matching user in org A as the positive control.
        gmarker = "zxqglobal"
        gproj_b = await _create_project_via_jwt(
            isolated_app, tenants["b"]["jwt"], f"Project {gmarker} B"
        )
        guser_b = await _create_named_user(
            superadmin_client,
            f"gsearch-{gmarker}-b-user",
            f"Gsearch {gmarker} User B",
        )
        gsess_b = await _create_session(
            auth_client_org_b, project_b, f"gsearch-{gmarker}-b-session"
        )
        guser_a = await _create_named_user(
            superadmin_client,
            f"gsearch-{gmarker}-a-user",
            f"Gsearch {gmarker} User A",
        )
        # Nameless + emailless user: external_id-only row must still match
        # via the external_id ILIKE leg and render with external_id label
        # (guards the label-fallback fix — the old code 500'd here).
        gnameless_a = await _create_user(
            superadmin_client, f"gsearch-{gmarker}-a-nameless"
        )

        gresp = await auth_client_org_a.get(
            "/v1/search", params={"q": gmarker, "limit": 50}
        )
        assert gresp.status_code == 200, f"Global search failed: {gresp.text}"
        gbody = gresp.json()
        assert gbody["query"] == gmarker
        found_ids = {r["id"] for r in gbody["results"]}
        assert gproj_b["id"] not in found_ids, "Global search leaked org-B project"
        assert guser_b["id"] not in found_ids, "Global search leaked org-B user"
        assert gsess_b["id"] not in found_ids, "Global search leaked org-B session"
        assert guser_a["id"] in found_ids, (
            "Positive control missing — leakage asserts vacuous"
        )
        by_id = {r["id"]: r for r in gbody["results"]}
        assert by_id[gnameless_a["id"]]["label"] == gnameless_a["external_id"], (
            "Nameless user label must fall back to external_id"
        )
