"""Integration tests for the enrichment skip-flag + session facts (B4/B5).

Covers the user-dictated list:

B4. Seed one session with 3 episodes (fully-earned w/ rows; gate-stamped
    full LLM bits + zero rows; in-flight partial bits) →
    ``GET .../sessions/{sid}/messages`` asserts ``is_enrichment_skipped``
    ``(False, True, False)`` + ``enrichment_status`` passthrough.

B5. ``GET .../sessions/{sid}/facts?episode_id=`` returns only own-episode
    facts (sibling excluded); bad UUID → 422; unknown UUID → 200 empty.

Seeding is via raw SQL (no workers run in test): episodes are inserted
directly with explicit ``enrichment_status`` bitmasks, and the earned
episode gets one ``facts`` row.  The ``user_id`` for every seeded row is
the tenant id from ``isolated_org_and_key`` — the same id the
``MemoryService``/fact workers persist under (see
``test_enrichment_gate._fact_count`` filtering ``Fact.user_id == org_id``).

B4 wires the real graph path (session testcontainer FalkorDB +
prod dispatcher on ``app.state``) so the stamped episode's entity-link
check resolves to a real empty result instead of "unknown".
"""

from __future__ import annotations

import urllib.parse
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

# Bits {0,2,3,4,5} — the SKIPPED_LLM_BITS_MASK value (1+4+8+16+32).
_FULL_LLM_BITS = 61
_PARTIAL_BITS = 1 << 0


async def _seed_episode(
    engine: Any,
    *,
    episode_id: UUID,
    session_id: UUID,
    project_id: UUID,
    user_id: UUID,
    sequence_number: int,
    bits: int,
) -> None:
    """Insert one episode row with an explicit enrichment bitmask."""
    async with AsyncSession(engine, expire_on_commit=False) as db:
        await db.execute(
            sa_text(
                "INSERT INTO episodes (id, user_id, project_id, session_id, "
                "role, content, token_count, sequence_number, "
                "enrichment_status) "
                "VALUES (:eid, :uid, :pid, :sid, 'user', :content, 0, "
                ":seq, :bits)"
            ),
            {
                "eid": episode_id,
                "uid": user_id,
                "pid": project_id,
                "sid": session_id,
                "content": f"episode {sequence_number}",
                "seq": sequence_number,
                "bits": bits,
            },
        )
        await db.commit()


async def _seed_fact(
    engine: Any,
    *,
    source_episode_id: UUID,
    project_id: UUID,
    user_id: UUID,
    content: str = "Alice likes hiking",
) -> UUID:
    """Insert one valid fact linked to ``source_episode_id``."""
    fact_id = uuid4()
    async with AsyncSession(engine, expire_on_commit=False) as db:
        await db.execute(
            sa_text(
                "INSERT INTO facts (id, user_id, project_id, content, "
                "subject, predicate, object, confidence, source_episode_id) "
                "VALUES (:fid, :uid, :pid, :content, 'Alice', 'likes', "
                "'hiking', 0.9, :eid)"
            ),
            {
                "fid": fact_id,
                "uid": user_id,
                "pid": project_id,
                "content": content,
                "eid": source_episode_id,
            },
        )
        await db.commit()
    return fact_id


async def _wire_real_graph(isolated_app: Any, falkordb_url: str) -> Any:
    """Point the app at the session FalkorDB testcontainer (prod dispatcher).

    Returns the client; the caller must close it (``await
    client.connection.aclose()``) at test end.
    """
    from core.graph_backend import init_dispatcher
    from falkordb.asyncio import FalkorDB
    from redis.asyncio import BlockingConnectionPool

    isolated_app.state.graph_backend_dispatcher = init_dispatcher()
    parsed = urllib.parse.urlparse(falkordb_url)
    pool = BlockingConnectionPool.from_url(
        falkordb_url, max_connections=5, decode_responses=True
    )
    client = FalkorDB(connection_pool=pool)
    isolated_app.state.falkordb_client = client
    return client


class TestEnrichmentSkippedFlag:
    """B4: messages carry the skip flag derived from bits + row presence."""

    async def test_messages_skip_flag_matrix(
        self,
        isolated_app: Any,
        isolated_auth_client: AsyncClient,
        isolated_org_and_key: dict,
        isolated_project_id: UUID,
        falkordb_url: str,
        engine: Any,
    ) -> None:
        """earned → False; gate-stamped (bits, no rows) → True; partial → False."""
        user_id: UUID = isolated_org_and_key["user_id"]
        project_id: UUID = isolated_project_id

        client = await _wire_real_graph(isolated_app, falkordb_url)
        try:
            created = await isolated_auth_client.post(
                f"/v1/projects/{project_id}/sessions",
                json={"external_id": "skip-flag-session"},
            )
            assert created.status_code == 201, f"session seed failed: {created.text}"
            session_id = UUID(created.json()["id"])

            earned_id, stamped_id, partial_id = uuid4(), uuid4(), uuid4()
            await _seed_episode(
                engine,
                episode_id=earned_id,
                session_id=session_id,
                project_id=project_id,
                user_id=user_id,
                sequence_number=0,
                bits=_FULL_LLM_BITS,
            )
            await _seed_episode(
                engine,
                episode_id=stamped_id,
                session_id=session_id,
                project_id=project_id,
                user_id=user_id,
                sequence_number=1,
                bits=_FULL_LLM_BITS,
            )
            await _seed_episode(
                engine,
                episode_id=partial_id,
                session_id=session_id,
                project_id=project_id,
                user_id=user_id,
                sequence_number=2,
                bits=_PARTIAL_BITS,
            )
            # Only the earned episode has LLM-produced rows.
            await _seed_fact(
                engine,
                source_episode_id=earned_id,
                project_id=project_id,
                user_id=user_id,
            )

            resp = await isolated_auth_client.get(
                f"/v1/projects/{project_id}/sessions/{session_id}/messages",
                params={"limit": 100},
            )
            assert resp.status_code == 200, (
                f"Expected 200, got {resp.status_code}: {resp.text}"
            )
            items = resp.json()["data"]
            assert len(items) == 3
            by_id = {item["id"]: item for item in items}

            assert by_id[str(earned_id)]["is_enrichment_skipped"] is False
            assert by_id[str(stamped_id)]["is_enrichment_skipped"] is True
            assert by_id[str(partial_id)]["is_enrichment_skipped"] is False

            assert by_id[str(earned_id)]["enrichment_status"] == _FULL_LLM_BITS
            assert by_id[str(stamped_id)]["enrichment_status"] == _FULL_LLM_BITS
            assert by_id[str(partial_id)]["enrichment_status"] == _PARTIAL_BITS
        finally:
            await client.connection.aclose()


class TestSessionFactsByEpisode:
    """B5: session-scoped facts read filtered by ``episode_id``."""

    async def test_facts_filtered_to_own_episode(
        self,
        isolated_auth_client: AsyncClient,
        isolated_org_and_key: dict,
        isolated_project_id: UUID,
        engine: Any,
    ) -> None:
        """Only facts from the requested episode are returned; sibling excluded."""
        user_id: UUID = isolated_org_and_key["user_id"]
        project_id: UUID = isolated_project_id

        created = await isolated_auth_client.post(
            f"/v1/projects/{project_id}/sessions",
            json={"external_id": "facts-scope-session"},
        )
        assert created.status_code == 201, f"session seed failed: {created.text}"
        session_id = UUID(created.json()["id"])

        own_id, sibling_id = uuid4(), uuid4()
        for seq, eid in ((0, own_id), (1, sibling_id)):
            await _seed_episode(
                engine,
                episode_id=eid,
                session_id=session_id,
                project_id=project_id,
                user_id=user_id,
                sequence_number=seq,
                bits=0,
            )
        await _seed_fact(
            engine,
            source_episode_id=own_id,
            project_id=project_id,
            user_id=user_id,
            content="Alice likes hiking",
        )
        await _seed_fact(
            engine,
            source_episode_id=sibling_id,
            project_id=project_id,
            user_id=user_id,
            content="Bob enjoys biking",
        )

        resp = await isolated_auth_client.get(
            f"/v1/projects/{project_id}/sessions/{session_id}/facts",
            params={"episode_id": str(own_id)},
        )
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text}"
        )
        items = resp.json()["data"]
        assert len(items) == 1
        assert items[0]["content"] == "Alice likes hiking"
        assert items[0]["source_episode_id"] == str(own_id)

        # Sibling episode returns only its own fact.
        sibling_resp = await isolated_auth_client.get(
            f"/v1/projects/{project_id}/sessions/{session_id}/facts",
            params={"episode_id": str(sibling_id)},
        )
        assert sibling_resp.status_code == 200
        sibling_items = sibling_resp.json()["data"]
        assert len(sibling_items) == 1
        assert sibling_items[0]["source_episode_id"] == str(sibling_id)

    async def test_facts_bad_episode_uuid_returns_422(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
    ) -> None:
        """A non-UUID ``episode_id`` fails request validation."""
        created = await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/sessions",
            json={"external_id": "facts-422-session"},
        )
        assert created.status_code == 201
        session_id = created.json()["id"]

        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/sessions/{session_id}/facts",
            params={"episode_id": "not-a-uuid"},
        )
        assert resp.status_code == 422, (
            f"Expected 422, got {resp.status_code}: {resp.text}"
        )

    async def test_facts_unknown_episode_uuid_returns_empty(
        self,
        isolated_auth_client: AsyncClient,
        isolated_org_and_key: dict,
        isolated_project_id: UUID,
        engine: Any,
    ) -> None:
        """A well-formed but unknown ``episode_id`` matches nothing → 200 empty."""
        user_id: UUID = isolated_org_and_key["user_id"]
        project_id: UUID = isolated_project_id

        created = await isolated_auth_client.post(
            f"/v1/projects/{project_id}/sessions",
            json={"external_id": "facts-empty-session"},
        )
        assert created.status_code == 201
        session_id = UUID(created.json()["id"])

        episode_id = uuid4()
        await _seed_episode(
            engine,
            episode_id=episode_id,
            session_id=session_id,
            project_id=project_id,
            user_id=user_id,
            sequence_number=0,
            bits=0,
        )

        resp = await isolated_auth_client.get(
            f"/v1/projects/{project_id}/sessions/{session_id}/facts",
            params={"episode_id": str(uuid4())},
        )
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text}"
        )
        assert resp.json()["data"] == []
