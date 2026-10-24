"""User-dictated integration test #5: ingest dedup 48h TTL.

Covers:
    - Same batch ingested twice <48h → same ``job_id`` replay (no dupes).
    - Claim backdated >48h (via SQL) → new ``job_id`` + new episode.

Real Postgres + Redis via testcontainers; real HTTP via ASGITransport.
No mocks.  Redis is flushed before the expired-retry so the DB-row
expiry (not the Redis fast-path entry) is what the test exercises.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
import uuid
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import text as sqltext

from tests.integration.conftest import asgi_transport


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

_MESSAGES = [
    {"role": "user", "content": "Dedup TTL probe one"},
    {"role": "assistant", "content": "Dedup TTL probe two"},
]


async def _ingest(
    client: AsyncClient,
    project_id: UUID,
    external_id: str,
    session_id: str,
) -> dict:
    """Ingest the fixed batch with a fresh Idempotency-Key; return the body."""
    resp = await client.post(
        f"/v1/projects/{project_id}/memory",
        headers={"Idempotency-Key": f"dedup-ttl-{uuid.uuid4().hex}"},
        data={
            "data": json.dumps(
                {
                    "external_id": external_id,
                    "session_id": session_id,
                    "messages": _MESSAGES,
                }
            )
        },
    )
    assert resp.status_code == 202, f"ingest failed: {resp.text}"
    return resp.json()


async def _message_count(client: AsyncClient, project_id: UUID, session_id: str) -> int:
    """Total visible messages for a session external ID."""
    resp = await client.get(f"/v1/projects/{project_id}/sessions?search={session_id}")
    assert resp.status_code == 200, resp.text
    return sum(s.get("message_count", 0) for s in resp.json().get("data", []))


async def _wait_for_count(
    client: AsyncClient,
    project_id: UUID,
    session_id: str,
    expected: int,
    timeout_s: float = 10.0,
) -> int:
    """Poll until the session shows ``expected`` messages."""
    deadline = time.monotonic() + timeout_s
    last = 0
    while time.monotonic() < deadline:
        last = await _message_count(client, project_id, session_id)
        if last >= expected:
            return last
        await asyncio.sleep(0.2)
    return last


class TestDedupTtl:
    """Content-dedup claims expire after 48h; replays inside it dedupe."""

    @pytest.mark.asyncio
    async def test_replay_inside_ttl_then_new_job_after_expiry(
        self,
        isolated_app: Any,
        isolated_auth_client: AsyncClient,
        isolated_org_and_key: dict,
        isolated_project_id: UUID,
        redis_client: Any,
    ) -> None:
        """Same batch: replay → same job; backdated claim → new job + episode."""
        client = isolated_auth_client
        external_id = f"dedup_ttl_user_{uuid.uuid4().hex[:8]}"
        session_id = f"dedup_ttl_session_{uuid.uuid4().hex[:8]}"

        # note: project-scoped API keys carry only project:* vocabulary,
        # so user/session setup goes through the admin JWT (members:write).
        transport = asgi_transport(isolated_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as admin_client:
            admin_client.headers["Authorization"] = (
                f"Bearer {isolated_org_and_key['jwt']}"
            )
            user_resp = await admin_client.post(
                "/v1/users", json={"external_id": external_id}
            )
            assert user_resp.status_code == 201, user_resp.text
            session_resp = await admin_client.post(
                f"/v1/projects/{isolated_project_id}/sessions",
                json={"external_id": session_id},
            )
            assert session_resp.status_code == 201, session_resp.text

        first = await _ingest(client, isolated_project_id, external_id, session_id)
        job_a = first["job_id"]
        assert await _wait_for_count(
            client, isolated_project_id, session_id, len(_MESSAGES)
        ) == len(_MESSAGES)

        second = await _ingest(client, isolated_project_id, external_id, session_id)
        assert second["job_id"] == job_a, (
            "replay inside the TTL must return the same job_id"
        )
        assert await _message_count(client, isolated_project_id, session_id) == len(
            _MESSAGES
        ), "replay must not create duplicate episodes"

        # Expire the claim — backdate the DB row past the 48h window.
        async with (
            isolated_app.state.db_session_factory() as db_session,
            db_session.begin(),
        ):
            await db_session.execute(
                sqltext(
                    "UPDATE ingest_dedup SET created_at = "
                    "now() - interval '49 hours' WHERE job_id = :jid"
                ),
                {"jid": str(job_a)},
            )
        # Drop the Redis fast-path entry so the DB row expiry decides.
        await redis_client.flushall()

        third = await _ingest(client, isolated_project_id, external_id, session_id)
        assert third["job_id"] != job_a, "expired claim must mint a new job_id"
        assert await _wait_for_count(
            client, isolated_project_id, session_id, 2 * len(_MESSAGES)
        ) == 2 * len(_MESSAGES), "expired claim must create a new episode"
