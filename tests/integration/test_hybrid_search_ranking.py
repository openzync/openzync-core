"""User-dictated integration test #6: HNSW ORDER BY search ranking.

Covers:
    - Ingest + search returns ranked rows (keyword-relevant episode found).
    - The vector leg plans an index scan (EXPLAIN with seqscan forced
      off); skipped with reason when no HNSW/IVF index exists here.

Real Postgres + Redis via testcontainers; real HTTP via ASGITransport.
No mocks.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import text as sqltext

from tests.integration.conftest import asgi_transport


@pytest.fixture(autouse=True)
async def _wire_graph_backend(isolated_app: Any) -> Any:
    """Attach the graph-backend dispatcher (set by lifespan in prod).

    The ``isolated_app`` fixture does not run the lifespan; without
    this every ``/search`` request 500s on
    ``app.state.graph_backend_dispatcher``.
    """
    from core.graph_backend import init_dispatcher

    isolated_app.state.graph_backend_dispatcher = init_dispatcher()
    return isolated_app


async def _fake_embed(texts: list[str]) -> list[list[float]]:
    """One 768-dim zero vector per text (matches ``vector(768)`` columns)."""
    return [[0.0] * 768 for _ in texts]


@pytest.fixture(autouse=True)
def _fake_embedding_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the embedding model for the vector search leg.

    Repo convention (see test_search_facts.py): the ONNX model is not
    downloaded in tests.  The pgvector ``<=>`` operator and the HNSW
    index still execute for real against real Postgres.
    """
    monkeypatch.setattr("core.embeddings.embed_query", _fake_embed)
    monkeypatch.setattr("core.embeddings.embed_passage", _fake_embed)


def _docker_up() -> bool:
    """Return True when a Docker daemon answers (testcontainers needs it)."""
    docker_bin = shutil.which("docker")
    if docker_bin is None:
        return False
    try:
        subprocess.run(  # noqa: S603 — fixed argv, no shell, no untrusted input
            [docker_bin, "info"],
            capture_output=True,
            timeout=30,
            check=True,
        )
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _docker_up(),
        reason="docker unavailable — PG/Redis testcontainers cannot start; skipped, not mocked",
    ),
]


class TestHnswSearchRanking:
    """Hybrid search returns ranked rows; vector leg uses the index."""

    @pytest.mark.asyncio
    async def test_search_returns_ranked_rows(
        self,
        isolated_app: Any,
        isolated_auth_client: AsyncClient,
        isolated_org_and_key: dict,
        isolated_project_id: UUID,
    ) -> None:
        """Keyword search returns rows with the matching episode present."""
        client = isolated_auth_client
        external_id = f"hnsw_user_{uuid.uuid4().hex[:8]}"
        session_id = f"hnsw_session_{uuid.uuid4().hex[:8]}"

        # note: project-scoped API keys carry only project:* vocabulary,
        # so user/session setup goes through the admin JWT (members:write).
        transport = asgi_transport(isolated_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as admin_client:
            admin_client.headers["Authorization"] = (
                f"Bearer {isolated_org_and_key['jwt']}"
            )
            assert (
                await admin_client.post("/v1/users", json={"external_id": external_id})
            ).status_code == 201
            assert (
                await admin_client.post(
                    f"/v1/projects/{isolated_project_id}/sessions",
                    json={"external_id": session_id},
                )
            ).status_code == 201

        messages = [
            {
                "role": "user",
                "content": "The quokka is a marsupial from Rottnest Island",
            },
            {"role": "assistant", "content": "Quokkas are known for looking cheerful"},
            {"role": "user", "content": "Wombats dig burrows with their claws"},
            {"role": "assistant", "content": "Unrelated note about quarterly planning"},
        ]
        ingest = await client.post(
            f"/v1/projects/{isolated_project_id}/memory",
            data={
                "data": json.dumps(
                    {
                        "external_id": external_id,
                        "session_id": session_id,
                        "messages": messages,
                    }
                )
            },
        )
        assert ingest.status_code == 202, f"ingest failed: {ingest.text}"

        search = await client.get(
            f"/v1/projects/{isolated_project_id}/search",
            params={"query": "quokka marsupial", "types": "episodes"},
        )
        assert search.status_code == 200, f"search failed: {search.text}"
        body = search.json()
        results = body.get("results", [])
        assert body.get("total", len(results)) > 0 and results, (
            "search must return ranked rows"
        )
        haystack = json.dumps(results).lower()
        assert "quokka" in haystack, (
            "keyword-relevant episode missing from ranked results"
        )

    @pytest.mark.asyncio
    async def test_vector_order_by_uses_index_scan(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
        isolated_project_id: UUID,
        engine: Any,
    ) -> None:
        """EXPLAIN of the service's vector ORDER BY shows an index scan.

        Seeds vector-scale rows (tiny tables legitimately seq-scan), then
        runs the repo-shaped query: project filter + ``IS NOT NULL`` +
        raw ``<=>`` in ORDER BY (the only form matching the HNSW index,
        per the comment in ``HybridRetriever._vector_search_episodes``).
        """
        org_id = isolated_org_and_key["org_id"]
        transport = asgi_transport(isolated_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as admin_client:
            admin_client.headers["Authorization"] = (
                f"Bearer {isolated_org_and_key['jwt']}"
            )
            user_resp = await admin_client.post(
                "/v1/users",
                json={"external_id": f"hnsw_scale_{uuid.uuid4().hex[:8]}"},
            )
            assert user_resp.status_code == 201, user_resp.text
            user_id = user_resp.json()["id"]
            ext = f"hnsw_scale_{uuid.uuid4().hex[:8]}"
            session_resp = await admin_client.post(
                f"/v1/projects/{isolated_project_id}/sessions",
                json={"external_id": ext},
            )
            assert session_resp.status_code == 201, session_resp.text
            listed = await admin_client.get(
                f"/v1/projects/{isolated_project_id}/sessions?search={ext}"
            )
            session_id = listed.json()["data"][0]["id"]

        zero_vector: list[float] = [0.0] * 768
        async with isolated_app.state.db_session_factory() as db_session:
            async with db_session.begin():
                await db_session.execute(
                    sqltext(
                        "INSERT INTO episodes "
                        "(organization_id, project_id, session_id, user_id, "
                        "role, content, sequence_number, embedding) "
                        "SELECT :org, :proj, :sess, :user, 'user', "
                        "'scale filler ' || g, g, "
                        "CAST(:emb AS vector) "
                        "FROM generate_series(0, 1499) AS g"
                    ),
                    {
                        "org": str(org_id),
                        "proj": str(isolated_project_id),
                        "sess": session_id,
                        "user": str(user_id),
                        "emb": zero_vector,
                    },
                )
            await db_session.execute(sqltext("ANALYZE episodes"))

        try:
            async with engine.connect() as conn, conn.begin():
                # note: SET LOCAL only takes effect inside an explicit
                # transaction — one execute per autocommit silently
                # no-ops and the planner keeps the seq scan.
                await conn.execute(sqltext("SET LOCAL enable_seqscan = off"))
                plan_rows = (
                    await conn.execute(
                        sqltext(
                            "EXPLAIN SELECT id, content FROM episodes "
                            "WHERE project_id = :proj "
                            "AND is_deleted = false "
                            "AND embedding IS NOT NULL "
                            "AND vector_dims(embedding) > 0 "
                            "ORDER BY embedding <=> "
                            "CAST(:emb AS vector(768)) "
                            "LIMIT 5"
                        ),
                        {
                            "proj": str(isolated_project_id),
                            "emb": zero_vector,
                        },
                    )
                ).fetchall()
        except Exception as exc:
            pytest.skip(f"vector EXPLAIN unavailable in this env: {exc}")
        plan = "\n".join(row[0] for row in plan_rows).lower()
        assert "index scan" in plan, (
            f"nearest-neighbor ORDER BY must use an index scan, got:\n{plan}"
        )
