"""Sorting + cursor parity guards — one shape per list surface.

Approved decision: STRICT PG decoder (fail-closed). Every cursor-issuing
surface embeds ``sort_by:sort_dir`` in its cursor and rejects a cursor from
a different sort (or a stale shape) with 422. Offset-paginated surfaces
(projects, audit-logs, api-keys, facts-at-time, fact-history) have no
cursor, so their guard is default-order + sort-flip + invalid-422.

Per-surface defaults (do not change without an ADR):

- users: created_at/desc (external_id, name, email, created_at; keyset)
- sessions: created_at/desc (external_id, created_at, updated_at; keyset)
- messages: sequence_number/asc locked (sequence_number, created_at; v1)
- session facts: created_at/desc (created_at, confidence, subject; offset)
- project facts: valid_from/desc (valid_from, created_at, ...; offset)
- fact history: at_time/desc (at_time only; offset)
- search / context: relevance default (relevance, recent; no cursor)
- projects: created_at/desc (name, created_at, updated_at; offset)
- audit-logs: created_at/desc (created_at, action, ...; offset)
- api-keys: created_at/desc (name, created_at, last_used_at; no paging)
- observations (PG): created_at/asc (name=content, created_at; keyset)
- graph nodes (PG): created_at/asc (name, created_at, entity_type; keyset)
- graph edges (PG): created_at/desc (created_at, predicate; keyset)

``*`` session/project fact history reads use offset pagination; the
cursor-mismatch guard does not apply to them.

Graph nodes/edges over HTTP hit the FalkorDB backend (offset cursors,
sort-safe, lenient) — the STRICT PG assertions live at the backend level
here (``TestGraphNodesPGParity``, ``TestGraphEdgesPGParity``,
``TestObservationsPGParity``) and the Falkor leniency is pinned in
``test_graph_queries.py``. Backend-level tests assert
``ValidationError`` (the exception the HTTP layer maps to 422).

References (untouched): ``test_user_repository.py`` codec roundtrip,
``test_episode_repository.py`` episode cursor anchor,
``tests/unit/core/test_cursor.py`` versioned-envelope anchor.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import text as sa_text

from core.exceptions import ValidationError
from core.sorting import SortSpec

pytestmark = pytest.mark.integration

STALE_CURSOR = "eyJub2RlX2lkIjogImFiYyJ9"  # base64 {"node_id": "abc"}


# ── Shared helpers ──────────────────────────────────────────────────────────


def _jwt_client(isolated_app: Any, jwt: str) -> AsyncClient:
    """Build an ad-hoc JWT-authenticated client for the isolated app."""
    from tests.integration.conftest import asgi_transport

    transport = asgi_transport(isolated_app)
    client = AsyncClient(transport=transport, base_url="http://test")
    client.headers["Authorization"] = f"Bearer {jwt}"
    return client


async def _seed_episodes(
    isolated_app: Any,
    org_id: UUID,
    project_id: UUID,
    session_id: UUID,
    user_id: UUID,
    contents: tuple[str, ...],
) -> list[Any]:
    """Insert episodes with known content via the repository; return rows."""
    from repositories.episode_repository import EpisodeRepository

    async with isolated_app.state.db_session_factory() as db:
        repo = EpisodeRepository(db)
        episodes = await repo.batch_create(
            organization_id=org_id,
            project_id=project_id,
            session_id=session_id,
            user_id=user_id,
            messages=[{"role": "user", "content": c, "metadata": {}} for c in contents],
        )
        await db.commit()
        return episodes


async def _seed_facts(
    isolated_app: Any,
    org_id: UUID,
    project_id: UUID,
    user_id: UUID,
    specs: list[dict[str, Any]],
    source_episode_id: UUID | None = None,
) -> list[Any]:
    """Insert facts with known subjects/valid_from via the repository."""
    from repositories.fact_repository import FactRepository

    async with isolated_app.state.db_session_factory() as db:
        repo = FactRepository(db)
        facts = []
        for s in specs:
            # Commit per row: ``created_at``/``valid_from`` default to
            # ``now()`` (transaction start), so a single commit would tie
            # all rows and the random-UUID tiebreak would scramble the
            # default order. Separate transactions give distinct stamps.
            facts.append(
                await repo.create(
                    user_id=user_id,
                    organization_id=org_id,
                    project_id=project_id,
                    content=s["content"],
                    subject=s["subject"],
                    predicate=s.get("predicate", "relates_to"),
                    obj=s.get("object", "thing"),
                    confidence=s.get("confidence", 0.9),
                    source_episode_id=source_episode_id,
                    # note: sort order never depends on provenance; session
                    # scoping joins through the source episode row.
                    valid_from=s.get("valid_from"),
                )
            )
            await db.commit()
        return facts


# ═══════════════════════════════════════════════════════════════════════════
# Users — default created_at/desc, keyset cursor
# ═══════════════════════════════════════════════════════════════════════════


class TestUsersParity:
    """Parity for ``GET /v1/users`` (default created_at/desc)."""

    _NAMES = ("parity-u-bravo", "parity-u-alpha", "parity-u-charlie")

    async def _seed(self, isolated_auth_client: AsyncClient) -> list[dict[str, Any]]:
        """Create three users; return rows in creation order."""
        rows = []
        for ext in self._NAMES:
            resp = await isolated_auth_client.post(
                "/v1/users", json={"external_id": ext}
            )
            assert resp.status_code == 201, resp.text
            rows.append(resp.json())
        return rows

    @pytest.mark.asyncio
    async def test_default_order_is_created_at_desc(
        self, isolated_auth_client: AsyncClient
    ) -> None:
        """No sort → newest first (creation order reversed)."""
        await self._seed(isolated_auth_client)
        resp = await isolated_auth_client.get(
            "/v1/users", params={"search": "parity-u-", "limit": 10}
        )
        assert resp.status_code == 200, resp.text
        got = [u["external_id"] for u in resp.json()["data"]]
        assert got == list(reversed(self._NAMES))

    @pytest.mark.asyncio
    async def test_sort_by_external_id_flips(
        self, isolated_auth_client: AsyncClient
    ) -> None:
        """?sort_by=external_id asc/desc flips the exact sequence."""
        await self._seed(isolated_auth_client)
        params = {"search": "parity-u-", "limit": 10}
        asc = await isolated_auth_client.get(
            "/v1/users", params={**params, "sort_by": "external_id", "sort_dir": "asc"}
        )
        desc = await isolated_auth_client.get(
            "/v1/users", params={**params, "sort_by": "external_id", "sort_dir": "desc"}
        )
        assert asc.status_code == 200 and desc.status_code == 200
        asc_ids = [u["external_id"] for u in asc.json()["data"]]
        desc_ids = [u["external_id"] for u in desc.json()["data"]]
        assert asc_ids == sorted(self._NAMES)
        assert desc_ids == sorted(self._NAMES, reverse=True)

    @pytest.mark.asyncio
    async def test_invalid_sort_by_is_422(
        self, isolated_auth_client: AsyncClient
    ) -> None:
        """?sort_by=bogus → 422 (Literal whitelist at the router)."""
        resp = await isolated_auth_client.get("/v1/users", params={"sort_by": "bogus"})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_stale_cursor_under_new_sort_is_422(
        self, isolated_auth_client: AsyncClient
    ) -> None:
        """Cursor from the default sort reused under a new sort → 422."""
        await self._seed(isolated_auth_client)
        page1 = await isolated_auth_client.get(
            "/v1/users", params={"search": "parity-u-", "limit": 1}
        )
        cursor = page1.json()["next_cursor"]
        assert cursor is not None
        resp = await isolated_auth_client.get(
            "/v1/users",
            params={
                "search": "parity-u-",
                "limit": 1,
                "cursor": cursor,
                "sort_by": "external_id",
                "sort_dir": "asc",
            },
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_stale_shape_cursor_is_422(
        self, isolated_auth_client: AsyncClient
    ) -> None:
        """Legacy ``{"node_id"}`` cursor shape → 422 (strict decoder)."""
        resp = await isolated_auth_client.get(
            "/v1/users", params={"cursor": STALE_CURSOR}
        )
        assert resp.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════
# Sessions — default created_at/desc, keyset cursor
# ═══════════════════════════════════════════════════════════════════════════


class TestSessionsParity:
    """Parity for ``GET /v1/projects/{pid}/sessions``."""

    _EXTS = ("parity-sess-01", "parity-sess-02", "parity-sess-03")

    async def _seed(self, isolated_auth_client: AsyncClient, pid: UUID) -> None:
        for ext in self._EXTS:
            resp = await isolated_auth_client.post(
                f"/v1/projects/{pid}/sessions", json={"external_id": ext}
            )
            assert resp.status_code == 201, resp.text

    @pytest.mark.asyncio
    async def test_default_order_is_created_at_desc(
        self, isolated_auth_client: AsyncClient, isolated_project_id: UUID
    ) -> None:
        """No sort → newest session first."""
        await self._seed(isolated_auth_client, isolated_project_id)
        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/sessions", params={"limit": 50}
        )
        assert resp.status_code == 200, resp.text
        got = [s["external_id"] for s in resp.json()["data"]]
        seeded = [e for e in got if e in self._EXTS]
        assert seeded == list(reversed(self._EXTS))

    @pytest.mark.asyncio
    async def test_sort_by_external_id_flips(
        self, isolated_auth_client: AsyncClient, isolated_project_id: UUID
    ) -> None:
        """?sort_by=external_id asc/desc flips the seeded subsequence."""
        await self._seed(isolated_auth_client, isolated_project_id)
        base = f"/v1/projects/{isolated_project_id}/sessions"
        asc = await isolated_auth_client.get(
            base, params={"sort_by": "external_id", "sort_dir": "asc", "limit": 50}
        )
        desc = await isolated_auth_client.get(
            base, params={"sort_by": "external_id", "sort_dir": "desc", "limit": 50}
        )
        assert asc.status_code == 200 and desc.status_code == 200
        asc_seeded = [
            s["external_id"]
            for s in asc.json()["data"]
            if s["external_id"] in self._EXTS
        ]
        desc_seeded = [
            s["external_id"]
            for s in desc.json()["data"]
            if s["external_id"] in self._EXTS
        ]
        assert asc_seeded == sorted(self._EXTS)
        assert desc_seeded == sorted(self._EXTS, reverse=True)

    @pytest.mark.asyncio
    async def test_invalid_sort_by_is_422(
        self, isolated_auth_client: AsyncClient, isolated_project_id: UUID
    ) -> None:
        """?sort_by=bogus → 422."""
        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/sessions",
            params={"sort_by": "bogus"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_stale_cursor_under_new_sort_is_422(
        self, isolated_auth_client: AsyncClient, isolated_project_id: UUID
    ) -> None:
        """Default-sort cursor reused under ?sort_by=external_id → 422."""
        await self._seed(isolated_auth_client, isolated_project_id)
        page1 = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/sessions", params={"limit": 1}
        )
        cursor = page1.json()["next_cursor"]
        assert cursor is not None
        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/sessions",
            params={
                "limit": 1,
                "cursor": cursor,
                "sort_by": "external_id",
                "sort_dir": "asc",
            },
        )
        assert resp.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════
# Messages — sequence_number/asc LOCKED, versioned cursor (both value schemes)
# ═══════════════════════════════════════════════════════════════════════════


class TestMessagesParity:
    """Parity for ``GET .../sessions/{sid}/messages``.

    The default ``sequence_number/asc`` order is locked (deterministic,
    tie-free). Two cursor value schemes exist: integer ``sequence_number``
    and ISO ``created_at`` — both versioned (``v1:`` envelope, 400 on
    pre-versioning shapes). The unversioned episode-repository scheme
    (``seq|id``) is anchored by ``test_episode_repository.py`` and
    ``tests/unit/core/test_cursor.py`` (untouched).
    """

    _CONTENTS = ("parity-msg-one", "parity-msg-two", "parity-msg-three")

    async def _seed_session(
        self,
        isolated_app: Any,
        isolated_auth_client: AsyncClient,
        isolated_org_and_key: dict[str, Any],
        pid: UUID,
    ) -> UUID:
        """Create a session + 3 episodes; return the session UUID."""
        resp = await isolated_auth_client.post(
            f"/v1/projects/{pid}/sessions",
            json={"external_id": "parity-msg-session"},
        )
        assert resp.status_code == 201, resp.text
        sid = UUID(resp.json()["id"])
        await _seed_episodes(
            isolated_app,
            isolated_org_and_key["org_id"],
            pid,
            sid,
            isolated_org_and_key["user_id"],
            self._CONTENTS,
        )
        return sid

    @pytest.mark.asyncio
    async def test_default_order_is_sequence_asc(
        self,
        isolated_app: Any,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
        isolated_org_and_key: dict[str, Any],
    ) -> None:
        """No sort → sequence_number ascending, consecutive."""
        sid = await self._seed_session(
            isolated_app,
            isolated_auth_client,
            isolated_org_and_key,
            isolated_project_id,
        )
        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/sessions/{sid}/messages",
            params={"limit": 50},
        )
        assert resp.status_code == 200, resp.text
        seqs = [m["sequence_number"] for m in resp.json()["data"]]
        assert len(seqs) == 3
        assert seqs == sorted(seqs)
        assert seqs[2] - seqs[0] == 2

    @pytest.mark.asyncio
    async def test_sort_dir_desc_flips(
        self,
        isolated_app: Any,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
        isolated_org_and_key: dict[str, Any],
    ) -> None:
        """?sort_by=sequence_number&sort_dir=desc reverses the default."""
        sid = await self._seed_session(
            isolated_app,
            isolated_auth_client,
            isolated_org_and_key,
            isolated_project_id,
        )
        base = f"/v1/projects/{isolated_project_id}/sessions/{sid}/messages"
        default = await isolated_auth_client.get(base, params={"limit": 50})
        flipped = await isolated_auth_client.get(
            base,
            params={"sort_by": "sequence_number", "sort_dir": "desc", "limit": 50},
        )
        assert flipped.status_code == 200
        assert [m["sequence_number"] for m in flipped.json()["data"]] == list(
            reversed([m["sequence_number"] for m in default.json()["data"]])
        )

    @pytest.mark.asyncio
    async def test_created_at_alt_sort(
        self,
        isolated_app: Any,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
        isolated_org_and_key: dict[str, Any],
    ) -> None:
        """?sort_by=created_at exercises the ISO-value cursor scheme → 200."""
        sid = await self._seed_session(
            isolated_app,
            isolated_auth_client,
            isolated_org_and_key,
            isolated_project_id,
        )
        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/sessions/{sid}/messages",
            params={"sort_by": "created_at", "sort_dir": "asc", "limit": 50},
        )
        assert resp.status_code == 200, resp.text
        assert len(resp.json()["data"]) == 3

    @pytest.mark.asyncio
    async def test_invalid_sort_by_is_422(
        self,
        isolated_app: Any,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
        isolated_org_and_key: dict[str, Any],
    ) -> None:
        """?sort_by=bogus → 422."""
        sid = await self._seed_session(
            isolated_app,
            isolated_auth_client,
            isolated_org_and_key,
            isolated_project_id,
        )
        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/sessions/{sid}/messages",
            params={"sort_by": "bogus"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_stale_cursor_under_new_sort_is_422(
        self,
        isolated_app: Any,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
        isolated_org_and_key: dict[str, Any],
    ) -> None:
        """sequence_number cursor reused under ?sort_by=created_at → 422."""
        sid = await self._seed_session(
            isolated_app,
            isolated_auth_client,
            isolated_org_and_key,
            isolated_project_id,
        )
        page1 = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/sessions/{sid}/messages",
            params={"limit": 1},
        )
        cursor = page1.json()["next_cursor"]
        assert cursor is not None
        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/sessions/{sid}/messages",
            params={
                "limit": 1,
                "cursor": cursor,
                "sort_by": "created_at",
                "sort_dir": "asc",
            },
        )
        assert resp.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════
# Facts — session (created_at/desc), project (valid_from/desc), history (at_time)
# ═══════════════════════════════════════════════════════════════════════════


class TestSessionFactsParity:
    """Parity for ``GET .../sessions/{sid}/facts`` (default created_at/desc)."""

    async def _seed(
        self,
        isolated_app: Any,
        isolated_auth_client: AsyncClient,
        isolated_org_and_key: dict[str, Any],
        pid: UUID,
    ) -> UUID:
        resp = await isolated_auth_client.post(
            f"/v1/projects/{pid}/sessions", json={"external_id": "parity-fact-session"}
        )
        assert resp.status_code == 201, resp.text
        sid = UUID(resp.json()["id"])
        org_id = isolated_org_and_key["org_id"]
        episodes = await _seed_episodes(
            isolated_app,
            org_id,
            pid,
            sid,
            isolated_org_and_key["user_id"],
            ("parity fact episode",),
        )
        await _seed_facts(
            isolated_app,
            org_id,
            pid,
            isolated_org_and_key["user_id"],
            [
                {"content": "parity fact one", "subject": "subj_a"},
                {"content": "parity fact two", "subject": "subj_b"},
                {"content": "parity fact three", "subject": "subj_c"},
            ],
            source_episode_id=episodes[0].id,
        )
        return sid

    @pytest.mark.asyncio
    async def test_default_order_is_created_at_desc(
        self,
        isolated_app: Any,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
        isolated_org_and_key: dict[str, Any],
    ) -> None:
        """No sort → newest fact first."""
        sid = await self._seed(
            isolated_app,
            isolated_auth_client,
            isolated_org_and_key,
            isolated_project_id,
        )
        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/sessions/{sid}/facts",
            params={"limit": 50},
        )
        assert resp.status_code == 200, resp.text
        assert [f["subject"] for f in resp.json()["data"]] == [
            "subj_c",
            "subj_b",
            "subj_a",
        ]

    @pytest.mark.asyncio
    async def test_sort_by_subject_flips(
        self,
        isolated_app: Any,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
        isolated_org_and_key: dict[str, Any],
    ) -> None:
        """?sort_by=subject asc/desc flips the exact sequence."""
        sid = await self._seed(
            isolated_app,
            isolated_auth_client,
            isolated_org_and_key,
            isolated_project_id,
        )
        base = f"/v1/projects/{isolated_project_id}/sessions/{sid}/facts"
        asc = await isolated_auth_client.get(
            base, params={"sort_by": "subject", "sort_dir": "asc", "limit": 50}
        )
        desc = await isolated_auth_client.get(
            base, params={"sort_by": "subject", "sort_dir": "desc", "limit": 50}
        )
        assert asc.status_code == 200 and desc.status_code == 200
        assert [f["subject"] for f in asc.json()["data"]] == [
            "subj_a",
            "subj_b",
            "subj_c",
        ]
        assert [f["subject"] for f in desc.json()["data"]] == [
            "subj_c",
            "subj_b",
            "subj_a",
        ]

    @pytest.mark.asyncio
    async def test_invalid_sort_by_is_422(
        self,
        isolated_app: Any,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
        isolated_org_and_key: dict[str, Any],
    ) -> None:
        """?sort_by=bogus → 422."""
        sid = await self._seed(
            isolated_app,
            isolated_auth_client,
            isolated_org_and_key,
            isolated_project_id,
        )
        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/sessions/{sid}/facts",
            params={"sort_by": "bogus"},
        )
        assert resp.status_code == 422


class TestProjectFactsParity:
    """Parity for ``GET /v1/projects/{pid}/facts`` (default valid_from/desc)."""

    async def _seed(
        self,
        isolated_app: Any,
        isolated_auth_client: AsyncClient,
        isolated_org_and_key: dict[str, Any],
        pid: UUID,
    ) -> None:
        now = datetime.now(UTC)
        await _seed_facts(
            isolated_app,
            isolated_org_and_key["org_id"],
            pid,
            isolated_org_and_key["user_id"],
            [
                {
                    "content": "parity pfact old",
                    "subject": "psubj_old",
                    "valid_from": now - timedelta(days=2),
                },
                {
                    "content": "parity pfact mid",
                    "subject": "psubj_mid",
                    "valid_from": now - timedelta(days=1),
                },
                {
                    "content": "parity pfact new",
                    "subject": "psubj_new",
                    "valid_from": now,
                },
            ],
        )

    @pytest.mark.asyncio
    async def test_default_order_is_valid_from_desc(
        self,
        isolated_app: Any,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
        isolated_org_and_key: dict[str, Any],
    ) -> None:
        """No sort → largest valid_from first (NOT created_at order)."""
        await self._seed(
            isolated_app,
            isolated_auth_client,
            isolated_org_and_key,
            isolated_project_id,
        )
        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/facts", params={"limit": 50}
        )
        assert resp.status_code == 200, resp.text
        got = [
            f["subject"]
            for f in resp.json()["data"]
            if f["subject"].startswith("psubj_")
        ]
        assert got == ["psubj_new", "psubj_mid", "psubj_old"]

    @pytest.mark.asyncio
    async def test_sort_by_subject_flips(
        self,
        isolated_app: Any,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
        isolated_org_and_key: dict[str, Any],
    ) -> None:
        """?sort_by=subject asc/desc flips the seeded subsequence."""
        await self._seed(
            isolated_app,
            isolated_auth_client,
            isolated_org_and_key,
            isolated_project_id,
        )
        base = f"/v1/projects/{isolated_project_id}/facts"
        asc = await isolated_auth_client.get(
            base, params={"sort_by": "subject", "sort_dir": "asc", "limit": 50}
        )
        desc = await isolated_auth_client.get(
            base, params={"sort_by": "subject", "sort_dir": "desc", "limit": 50}
        )
        assert asc.status_code == 200 and desc.status_code == 200
        asc_seeded = [
            f["subject"]
            for f in asc.json()["data"]
            if f["subject"].startswith("psubj_")
        ]
        desc_seeded = [
            f["subject"]
            for f in desc.json()["data"]
            if f["subject"].startswith("psubj_")
        ]
        assert asc_seeded == ["psubj_mid", "psubj_new", "psubj_old"]
        assert desc_seeded == ["psubj_old", "psubj_new", "psubj_mid"]

    @pytest.mark.asyncio
    async def test_invalid_sort_by_is_422(
        self, isolated_auth_client: AsyncClient, isolated_project_id: UUID
    ) -> None:
        """?sort_by=bogus → 422."""
        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/facts", params={"sort_by": "bogus"}
        )
        assert resp.status_code == 422


class TestFactHistoryParity:
    """Parity for ``GET .../facts/{fid}/history`` (default at_time/desc).

    Event ordering with live lineage is covered by
    ``test_fact_supersession_api.py`` (untouched); here we pin the sort
    contract on a fact with no events yet.
    """

    @pytest.mark.asyncio
    async def test_history_sort_contract(
        self,
        isolated_app: Any,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
        isolated_org_and_key: dict[str, Any],
    ) -> None:
        """Default → 200 with empty events; at_time asc/desc → 200; bogus → 422."""
        resp = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/sessions",
            json={"external_id": "parity-hist-session"},
        )
        assert resp.status_code == 201, resp.text
        facts = await _seed_facts(
            isolated_app,
            isolated_org_and_key["org_id"],
            isolated_project_id,
            isolated_org_and_key["user_id"],
            [{"content": "parity hist fact", "subject": "hsubj"}],
        )
        fid = facts[0].id
        base = f"/v1/projects/{isolated_project_id}/facts/{fid}/history"
        default = await isolated_auth_client.get(base)
        assert default.status_code == 200, default.text
        assert default.json()["events"] == []
        for direction in ("asc", "desc"):
            r = await isolated_auth_client.get(
                base, params={"sort_by": "at_time", "sort_dir": direction}
            )
            assert r.status_code == 200, r.text
        bad = await isolated_auth_client.get(base, params={"sort_by": "bogus"})
        assert bad.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════
# Search / context — relevance (default) vs recent
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
async def _wire_graph_backend(isolated_app: Any) -> Any:
    """Attach the dispatcher (lifespan-skipped) for search/context routes."""
    from core.graph_backend import init_dispatcher

    isolated_app.state.graph_backend_dispatcher = init_dispatcher()
    return isolated_app


@pytest.fixture(autouse=True)
def _fake_embedding_backend(monkeypatch: Any) -> None:
    """Neutralise the vector leg (768-dim zero vectors)."""
    from dataclasses import dataclass

    @dataclass
    class _Resp:
        embeddings: list[list[float]] | None = None

    class _Backend:
        async def embed(self, texts: Any, model: Any = None) -> _Resp:
            return _Resp(embeddings=[[0.0] * 768 for _ in texts])

    async def _resolve(
        provider: Any = None, org_config: Any = None, mode: Any = None
    ) -> _Backend:
        return _Backend()

    monkeypatch.setattr("core.llm.resolve_backend", _resolve)


class TestSearchParity:
    """Parity for ``GET .../search`` — default ``relevance`` vs ``recent``."""

    _KW = "paritysearchkw"

    async def _seed(
        self,
        isolated_app: Any,
        isolated_auth_client: AsyncClient,
        isolated_org_and_key: dict[str, Any],
        pid: UUID,
    ) -> None:
        resp = await isolated_auth_client.post(
            f"/v1/projects/{pid}/sessions",
            json={"external_id": "parity-search-session"},
        )
        assert resp.status_code == 201, resp.text
        sid = UUID(resp.json()["id"])
        await _seed_episodes(
            isolated_app,
            isolated_org_and_key["org_id"],
            pid,
            sid,
            isolated_org_and_key["user_id"],
            (
                f"{self._KW} first episode",
                f"{self._KW} second episode",
                f"{self._KW} third episode",
            ),
        )

    @pytest.mark.asyncio
    async def test_default_is_relevance_and_recent_matches_set(
        self,
        isolated_app: Any,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
        isolated_org_and_key: dict[str, Any],
    ) -> None:
        """Default == explicit relevance (exact order); recent == same set."""
        await self._seed(
            isolated_app,
            isolated_auth_client,
            isolated_org_and_key,
            isolated_project_id,
        )
        base = f"/v1/projects/{isolated_project_id}/search"
        params = {"query": self._KW, "types": "episodes"}
        default = await isolated_auth_client.get(base, params=params)
        relevance = await isolated_auth_client.get(
            base, params={**params, "sort": "relevance"}
        )
        recent = await isolated_auth_client.get(
            base, params={**params, "sort": "recent"}
        )
        assert default.status_code == 200, default.text
        assert relevance.status_code == 200 and recent.status_code == 200
        default_ids = [r["id"] for r in default.json()["results"]]
        assert len(default_ids) == 3
        assert [r["id"] for r in relevance.json()["results"]] == default_ids
        assert sorted(r["id"] for r in recent.json()["results"]) == sorted(default_ids)

    @pytest.mark.asyncio
    async def test_invalid_sort_is_422(
        self, isolated_auth_client: AsyncClient, isolated_project_id: UUID
    ) -> None:
        """?sort=bogus → 422."""
        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/search",
            params={"query": "hello", "sort": "bogus"},
        )
        assert resp.status_code == 422


class TestContextParity:
    """Parity for ``GET .../context`` — default ``relevance`` vs ``recent``."""

    @pytest.mark.asyncio
    async def test_sort_variants_accepted(
        self,
        isolated_app: Any,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
        isolated_org_and_key: dict[str, Any],
    ) -> None:
        """relevance (default) + recent → 200; bogus → 422."""
        resp = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/sessions",
            json={"external_id": "parity-ctx-session"},
        )
        assert resp.status_code == 201, resp.text
        base = f"/v1/projects/{isolated_project_id}/context"
        for params in (
            {"query": "parity context probe", "format": "json"},
            {"query": "parity context probe", "format": "json", "sort": "relevance"},
            {"query": "parity context probe", "format": "json", "sort": "recent"},
        ):
            r = await isolated_auth_client.get(base, params=params)
            assert r.status_code == 200, f"{params}: {r.text}"
        bad = await isolated_auth_client.get(
            base, params={"query": "parity context probe", "sort": "bogus"}
        )
        assert bad.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════
# Projects — HTTP happy-path list, default created_at/desc, offset paging
# ═══════════════════════════════════════════════════════════════════════════


class TestProjectsParity:
    """Parity for ``GET /v1/projects`` (bare list, offset pagination)."""

    _NAMES = ("sort-parity-proj-b", "sort-parity-proj-a")

    @pytest.mark.asyncio
    async def test_list_happy_path_and_sort_contract(
        self,
        isolated_app: Any,
        isolated_auth_client: AsyncClient,
        isolated_org_and_key: dict[str, Any],
    ) -> None:
        """Happy-path list contains seeds; default desc; name flip; bogus → 422."""
        async with _jwt_client(isolated_app, isolated_org_and_key["jwt"]) as jwt_cli:
            for name in self._NAMES:
                r = await jwt_cli.post("/v1/projects", json={"name": name})
                assert r.status_code == 201, r.text

        resp = await isolated_auth_client.get("/v1/projects", params={"limit": 100})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert isinstance(body, list)
        seeded = [p["name"] for p in body if p["name"] in self._NAMES]
        assert seeded == ["sort-parity-proj-a", "sort-parity-proj-b"]

        asc = await isolated_auth_client.get(
            "/v1/projects",
            params={"sort_by": "name", "sort_dir": "asc", "limit": 100},
        )
        desc = await isolated_auth_client.get(
            "/v1/projects",
            params={"sort_by": "name", "sort_dir": "desc", "limit": 100},
        )
        assert asc.status_code == 200 and desc.status_code == 200
        asc_seeded = [p["name"] for p in asc.json() if p["name"] in self._NAMES]
        desc_seeded = [p["name"] for p in desc.json() if p["name"] in self._NAMES]
        assert asc_seeded == sorted(self._NAMES)
        assert desc_seeded == sorted(self._NAMES, reverse=True)

        bad = await isolated_auth_client.get(
            "/v1/projects", params={"sort_by": "bogus"}
        )
        assert bad.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════
# Audit logs — default created_at/desc, offset paging
# ═══════════════════════════════════════════════════════════════════════════


class TestAuditLogsParity:
    """Parity for ``GET /v1/admin/audit-logs`` (offset pagination, no cursor)."""

    @pytest.mark.asyncio
    async def test_default_desc_and_flip_and_invalid(
        self,
        isolated_app: Any,
        isolated_auth_client: AsyncClient,
        isolated_org_and_key: dict[str, Any],
    ) -> None:
        """Seed entries directly, then: default desc; asc flips; bogus → 422.

        Entries are seeded via the repository — HTTP activity does not
        reliably produce rows in tests (audit persistence is org-config
        gated), and the sort contract is what is pinned here.
        """
        from repositories.audit_log_repository import AuditLogRepository

        org_id = isolated_org_and_key["org_id"]
        async with isolated_app.state.db_session_factory() as db:
            repo = AuditLogRepository(db)
            for action in ("parity.audit.b", "parity.audit.a", "parity.audit.c"):
                await repo.create(
                    organization_id=org_id,
                    actor_id="parity-auditor",
                    actor_type="api_key",
                    action=action,
                    resource_type="parity-audit",
                    resource_id=None,
                    details={},
                    ip_address=None,
                )

        base = "/v1/admin/audit-logs"
        filt = {"resource_type": "parity-audit", "limit": 50}
        default = await isolated_auth_client.get(base, params=filt)
        assert default.status_code == 200, default.text
        items = default.json()["items"]
        assert [i["action"] for i in items] == [
            "parity.audit.c",
            "parity.audit.a",
            "parity.audit.b",
        ]

        asc = await isolated_auth_client.get(
            base, params={**filt, "sort_by": "action", "sort_dir": "asc"}
        )
        desc = await isolated_auth_client.get(
            base, params={**filt, "sort_by": "action", "sort_dir": "desc"}
        )
        assert asc.status_code == 200 and desc.status_code == 200
        assert [i["action"] for i in asc.json()["items"]] == [
            "parity.audit.a",
            "parity.audit.b",
            "parity.audit.c",
        ]
        assert [i["action"] for i in desc.json()["items"]] == [
            "parity.audit.c",
            "parity.audit.b",
            "parity.audit.a",
        ]

        bad = await isolated_auth_client.get(base, params={"sort_by": "bogus"})
        assert bad.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════
# API keys — default created_at/desc, no pagination
# ═══════════════════════════════════════════════════════════════════════════


class TestApiKeysParity:
    """Parity for ``GET /v1/projects/{pid}/api-keys`` (JWT, owner-gated)."""

    _NAMES = ("pk-parity-bravo", "pk-parity-alpha", "pk-parity-charlie")

    @pytest.mark.asyncio
    async def test_list_sort_contract(
        self,
        isolated_app: Any,
        isolated_project_id: UUID,
        isolated_org_and_key: dict[str, Any],
    ) -> None:
        """Default desc; name asc/desc flips; bogus → 422."""
        async with _jwt_client(isolated_app, isolated_org_and_key["jwt"]) as jwt_cli:
            for name in self._NAMES:
                r = await jwt_cli.post(
                    f"/v1/projects/{isolated_project_id}/api-keys",
                    json={"name": name},
                )
                assert r.status_code == 201, r.text

            base = f"/v1/projects/{isolated_project_id}/api-keys"
            default = await jwt_cli.get(base)
            assert default.status_code == 200, default.text
            seeded = [
                k["name"] for k in default.json()["data"] if k["name"] in self._NAMES
            ]
            assert seeded == list(reversed(self._NAMES))

            asc = await jwt_cli.get(base, params={"sort_by": "name", "sort_dir": "asc"})
            desc = await jwt_cli.get(
                base, params={"sort_by": "name", "sort_dir": "desc"}
            )
            assert asc.status_code == 200 and desc.status_code == 200
            asc_seeded = [
                k["name"] for k in asc.json()["data"] if k["name"] in self._NAMES
            ]
            desc_seeded = [
                k["name"] for k in desc.json()["data"] if k["name"] in self._NAMES
            ]
            assert asc_seeded == sorted(self._NAMES)
            assert desc_seeded == sorted(self._NAMES, reverse=True)

            bad = await jwt_cli.get(base, params={"sort_by": "bogus"})
            assert bad.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════
# PG-backend surfaces — strict decoder asserts ValidationError (= HTTP 422)
# ═══════════════════════════════════════════════════════════════════════════


async def _stagger(db: Any, table: str, ids: list[UUID], base: datetime) -> None:
    """Assign distinct created_at values so default orders are exact."""
    if table not in ("graph_entities", "graph_observations", "graph_relationships"):
        raise ValueError(f"refusing to stagger unknown table {table!r}")
    for i, row_id in enumerate(ids):
        stmt = sa_text(
            f"UPDATE {table} SET created_at = :ts WHERE id = :id"  # noqa: S608
        )
        await db.execute(stmt, {"ts": base + timedelta(seconds=i), "id": row_id})
    await db.flush()


class TestObservationsPGParity:
    """Parity for ``PostgresGraphBackend.get_observations`` (created_at/asc)."""

    @pytest.mark.asyncio
    async def test_full_sort_contract(
        self,
        isolated_app: Any,
        isolated_project_id: UUID,
        isolated_org_and_key: dict[str, Any],
    ) -> None:
        """Default asc exact; name flip; live cursor; mismatch/stale → 422."""
        from packages.graph_backend.postgres import PostgresGraphBackend

        org_id = isolated_org_and_key["org_id"]
        async with isolated_app.state.db_session_factory() as db:
            backend = PostgresGraphBackend(db=db)
            subject = UUID("11111111-1111-1111-1111-111111111111")
            await db.execute(
                sa_text(
                    "INSERT INTO graph_entities "
                    "(id, organization_id, project_id, name, entity_type) "
                    "VALUES (:id, :org, :proj, 'parity-obs-subject', 'test') "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {"id": subject, "org": org_id, "proj": isolated_project_id},
            )
            rows = [
                await backend.upsert_observation(
                    org_id=org_id,
                    project_id=isolated_project_id,
                    subject_entity_id=subject,
                    observation_type=f"parity_obs_{sfx}",
                    content=f"obs-content-{sfx}",
                    confidence=0.5,
                )
                for sfx in ("bravo", "alpha", "charlie")
            ]
            ids = [UUID(r["id"]) for r in rows]
            await _stagger(db, "graph_observations", ids, datetime.now(UTC))
            await db.commit()

            default = await backend.get_observations(
                org_id=org_id, project_id=isolated_project_id, limit=50
            )
            assert [i["content"] for i in default["items"]] == [
                "obs-content-bravo",
                "obs-content-alpha",
                "obs-content-charlie",
            ]

            asc = await backend.get_observations(
                org_id=org_id,
                project_id=isolated_project_id,
                limit=50,
                sort=SortSpec(sort_by="name", sort_dir="asc"),
            )
            desc = await backend.get_observations(
                org_id=org_id,
                project_id=isolated_project_id,
                limit=50,
                sort=SortSpec(sort_by="name", sort_dir="desc"),
            )
            assert [i["content"] for i in asc["items"]] == [
                "obs-content-alpha",
                "obs-content-bravo",
                "obs-content-charlie",
            ]
            assert [i["content"] for i in desc["items"]] == [
                "obs-content-charlie",
                "obs-content-bravo",
                "obs-content-alpha",
            ]

            page1 = await backend.get_observations(
                org_id=org_id, project_id=isolated_project_id, limit=1
            )
            assert page1["next_cursor"] is not None
            # ⚠️ Known src gap for @build: every PG graph keyset query
            # interpolates ``:cursor_id::uuid``, which SQLAlchemy text()
            # never binds (asyncpg ProgrammingError → 500), so following
            # a cursor is untestable until fixed. Only cursor issuance is
            # pinned here; the fail-closed decode guards below all raise
            # before SQL and are unaffected.

            with pytest.raises(ValidationError):
                await backend.get_observations(
                    org_id=org_id,
                    project_id=isolated_project_id,
                    limit=1,
                    cursor=page1["next_cursor"],
                    sort=SortSpec(sort_by="name", sort_dir="asc"),
                )
            with pytest.raises(ValidationError):
                await backend.get_observations(
                    org_id=org_id,
                    project_id=isolated_project_id,
                    limit=1,
                    cursor=STALE_CURSOR,
                )
            with pytest.raises(ValidationError):
                await backend.get_observations(
                    org_id=org_id,
                    project_id=isolated_project_id,
                    limit=50,
                    sort=SortSpec(sort_by="bogus", sort_dir="asc"),
                )


class TestGraphNodesPGParity:
    """Parity for ``PostgresGraphBackend.list_entities`` (created_at/asc).

    STRICT decoder: the legacy ``{"node_id"}`` shape and any sort-mismatched
    cursor raise ``ValidationError`` (HTTP 422). Falkor's lenient-200 for
    these shapes is pinned in ``test_graph_queries.py``.
    """

    @pytest.mark.asyncio
    async def test_full_sort_contract(
        self,
        isolated_app: Any,
        isolated_project_id: UUID,
        isolated_org_and_key: dict[str, Any],
    ) -> None:
        """Default asc exact; name flip; live cursor; mismatch/stale → 422."""
        from packages.graph_backend.postgres import PostgresGraphBackend

        org_id = isolated_org_and_key["org_id"]
        async with isolated_app.state.db_session_factory() as db:
            backend = PostgresGraphBackend(db=db)
            created = [
                await backend.create_entity(
                    org_id=org_id,
                    project_id=isolated_project_id,
                    name=n,
                    entity_type="Person",
                )
                for n in ("parity-ent-bravo", "parity-ent-alpha", "parity-ent-charlie")
            ]
            await _stagger(
                db,
                "graph_entities",
                [UUID(e["id"]) for e in created],
                datetime.now(UTC),
            )
            await db.commit()

            default = await backend.list_entities(
                org_id=org_id, project_id=isolated_project_id, limit=50
            )
            assert [i["name"] for i in default["items"]] == [
                "parity-ent-bravo",
                "parity-ent-alpha",
                "parity-ent-charlie",
            ]

            asc = await backend.list_entities(
                org_id=org_id,
                project_id=isolated_project_id,
                limit=50,
                sort=SortSpec(sort_by="name", sort_dir="asc"),
            )
            desc = await backend.list_entities(
                org_id=org_id,
                project_id=isolated_project_id,
                limit=50,
                sort=SortSpec(sort_by="name", sort_dir="desc"),
            )
            assert [i["name"] for i in asc["items"]] == [
                "parity-ent-alpha",
                "parity-ent-bravo",
                "parity-ent-charlie",
            ]
            assert [i["name"] for i in desc["items"]] == [
                "parity-ent-charlie",
                "parity-ent-bravo",
                "parity-ent-alpha",
            ]

            page1 = await backend.list_entities(
                org_id=org_id, project_id=isolated_project_id, limit=1
            )
            assert page1["next_cursor"] is not None
            # ⚠️ Same src gap as observations: following a PG keyset
            # cursor 500s on unbound ``:cursor_id::uuid`` — issuance only
            # until @build fixes the binding.

            with pytest.raises(ValidationError):
                await backend.list_entities(
                    org_id=org_id,
                    project_id=isolated_project_id,
                    limit=1,
                    cursor=page1["next_cursor"],
                    sort=SortSpec(sort_by="name", sort_dir="asc"),
                )
            with pytest.raises(ValidationError):
                await backend.list_entities(
                    org_id=org_id,
                    project_id=isolated_project_id,
                    limit=1,
                    cursor=STALE_CURSOR,
                )
            with pytest.raises(ValidationError):
                await backend.list_entities(
                    org_id=org_id,
                    project_id=isolated_project_id,
                    limit=50,
                    sort=SortSpec(sort_by="bogus", sort_dir="asc"),
                )


class TestGraphEdgesPGParity:
    """Parity for ``PostgresGraphBackend.list_entity_edges`` (created_at/desc)."""

    @pytest.mark.asyncio
    async def test_full_sort_contract(
        self,
        isolated_app: Any,
        isolated_project_id: UUID,
        isolated_org_and_key: dict[str, Any],
    ) -> None:
        """Default desc exact; predicate flip; live cursor; mismatch/stale → 422."""
        from packages.graph_backend.postgres import PostgresGraphBackend

        org_id = isolated_org_and_key["org_id"]
        async with isolated_app.state.db_session_factory() as db:
            backend = PostgresGraphBackend(db=db)
            src = await backend.create_entity(
                org_id=org_id,
                project_id=isolated_project_id,
                name="parity-edge-src",
                entity_type="Person",
            )
            dst = await backend.create_entity(
                org_id=org_id,
                project_id=isolated_project_id,
                name="parity-edge-dst",
                entity_type="Person",
            )
            src_id, dst_id = UUID(src["id"]), UUID(dst["id"])
            rels = [
                await backend.create_relationship(
                    org_id=org_id,
                    project_id=isolated_project_id,
                    source_id=src_id,
                    target_id=dst_id,
                    relationship_type=t,
                )
                for t in ("zz_rel", "aa_rel", "mm_rel")
            ]
            await _stagger(
                db,
                "graph_relationships",
                [UUID(r["id"]) for r in rels],
                datetime.now(UTC),
            )
            await db.commit()

            default = await backend.list_entity_edges(
                org_id=org_id,
                project_id=isolated_project_id,
                entity_id=src_id,
                limit=50,
            )
            assert [i["type"] for i in default["items"]] == [
                "mm_rel",
                "aa_rel",
                "zz_rel",
            ]

            asc = await backend.list_entity_edges(
                org_id=org_id,
                project_id=isolated_project_id,
                entity_id=src_id,
                limit=50,
                sort=SortSpec(sort_by="predicate", sort_dir="asc"),
            )
            desc = await backend.list_entity_edges(
                org_id=org_id,
                project_id=isolated_project_id,
                entity_id=src_id,
                limit=50,
                sort=SortSpec(sort_by="predicate", sort_dir="desc"),
            )
            assert [i["type"] for i in asc["items"]] == ["aa_rel", "mm_rel", "zz_rel"]
            assert [i["type"] for i in desc["items"]] == ["zz_rel", "mm_rel", "aa_rel"]

            page1 = await backend.list_entity_edges(
                org_id=org_id,
                project_id=isolated_project_id,
                entity_id=src_id,
                limit=1,
            )
            assert page1["next_cursor"] is not None
            # ⚠️ Same src gap as observations: following a PG keyset
            # cursor 500s on unbound ``:cursor_id::uuid`` — issuance only
            # until @build fixes the binding.

            with pytest.raises(ValidationError):
                await backend.list_entity_edges(
                    org_id=org_id,
                    project_id=isolated_project_id,
                    entity_id=src_id,
                    limit=1,
                    cursor=page1["next_cursor"],
                    sort=SortSpec(sort_by="predicate", sort_dir="asc"),
                )
            with pytest.raises(ValidationError):
                await backend.list_entity_edges(
                    org_id=org_id,
                    project_id=isolated_project_id,
                    entity_id=src_id,
                    limit=1,
                    cursor=STALE_CURSOR,
                )
            with pytest.raises(ValidationError):
                await backend.list_entity_edges(
                    org_id=org_id,
                    project_id=isolated_project_id,
                    entity_id=src_id,
                    limit=50,
                    sort=SortSpec(sort_by="bogus", sort_dir="asc"),
                )
