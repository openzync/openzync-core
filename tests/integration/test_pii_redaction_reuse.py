"""User-dictated integration test #7: PII reuse.

Covers:
    - Ingest N messages with PII on (default ``mask`` mode) → fast 202s.
    - Stored episodes carry redaction markers (``[REDACTED:*]``); raw
      PII values are absent from what the API returns.

Real Postgres + Redis via testcontainers; real HTTP via ASGITransport.
No mocks.  PII masking is on by default (regex layer, no spaCy model
needed at ``low`` sensitivity).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import uuid
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient

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

_RAW_EMAILS = ("alice.reuse@example.com", "bob.reuse@corp.io")
_RAW_IP = "192.168.1.10"
_MESSAGES = [
    {"role": "user", "content": f"Contact {_RAW_EMAILS[0]} for access"},
    {"role": "assistant", "content": f"Backup contact is {_RAW_EMAILS[1]}"},
    {"role": "user", "content": f"Server at {_RAW_IP} went down again"},
]


class TestPiiReuse:
    """PII is masked at ingest; markers (not raw values) are stored."""

    @pytest.mark.asyncio
    async def test_ingest_masks_pii_with_markers(
        self,
        isolated_app: Any,
        isolated_auth_client: AsyncClient,
        isolated_org_and_key: dict,
        isolated_project_id: UUID,
    ) -> None:
        """N PII messages → fast 202s + ``[REDACTED:*]`` in stored rows."""
        client = isolated_auth_client
        external_id = f"pii_user_{uuid.uuid4().hex[:8]}"
        session_id = f"pii_session_{uuid.uuid4().hex[:8]}"

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

        for message in _MESSAGES:
            started = time.monotonic()
            resp = await client.post(
                f"/v1/projects/{isolated_project_id}/memory",
                data={
                    "data": json.dumps(
                        {
                            "external_id": external_id,
                            "session_id": session_id,
                            "messages": [message],
                        }
                    )
                },
            )
            elapsed_ms = (time.monotonic() - started) * 1000
            assert resp.status_code == 202, f"ingest failed: {resp.text}"
            assert elapsed_ms < 2000, f"PII ingest too slow: {elapsed_ms:.1f}ms"

        sessions = await client.get(
            f"/v1/projects/{isolated_project_id}/sessions?search={session_id}"
        )
        assert sessions.status_code == 200
        matches = [
            s for s in sessions.json().get("data", []) if s["external_id"] == session_id
        ]
        assert matches, "ingested session missing"
        messages_resp = await client.get(
            f"/v1/projects/{isolated_project_id}/sessions/{matches[0]['id']}/messages"
        )
        assert messages_resp.status_code == 200
        dump = json.dumps(messages_resp.json())

        assert "[REDACTED:EMAIL]" in dump, (
            "stored episodes must carry email redaction markers"
        )
        assert "[REDACTED:IP]" in dump, (
            "stored episodes must carry IP redaction markers"
        )
        for raw in (*_RAW_EMAILS, _RAW_IP):
            assert raw not in dump, f"raw PII leaked into stored rows: {raw}"
