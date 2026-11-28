"""Integration tests for the enrichment gate (live PG15 + Redis).

Covers the user-dictated list, items 8–12.  Real Postgres rows, Redis,
RLS tenant binding, and the real ``enrich_episode`` worker run in every
test; only the two hermetic seams are stubbed:

- the LLM backend (``core.llm.resolve_backend`` + a ``make_sink`` spy —
  no API keys in test, same as ``test_enrichment_toggles.py``);
- the gate scorer (``core.enrichment_gate.should_skip`` for the
  skip/run outcomes; the real ``should_skip`` with a broken ``_load_pair``
  for the fail-open path).

No Hub network and no model download in any test: ``urllib`` is guarded
in the system-config tests, and gate scoring never leaves the stubbed
seams.  Embed/link work is untouched by design — the gate only ever
stamps the five LLM bits and returns before the LLM call, while
embed/link travel in separate ARQ payloads (see ``ingest.py`` fan-out).
"""

from __future__ import annotations

import json
import urllib.request
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select, text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from core.llm import ChatResponse, TokenUsage, UsageRecord
from models.episode import Episode
from models.fact import Fact
from schemas.llm_outputs import CombinedLLMOutput
from tests.integration.conftest import asgi_transport
from workers.tasks.base import (
    ENRICHMENT_CLASSIFICATION,
    ENRICHMENT_ENTITIES,
    ENRICHMENT_FACTS,
    ENRICHMENT_STRUCTURED_EXTRACTION,
    LLM_INVALIDATION_BIT,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_LLM_BITS = (
    ENRICHMENT_ENTITIES
    | ENRICHMENT_FACTS
    | ENRICHMENT_CLASSIFICATION
    | ENRICHMENT_STRUCTURED_EXTRACTION
    | LLM_INVALIDATION_BIT
)

_GATE_ON = {"enabled": True, "threshold": 0.5, "model": "bogus/gate-stubbed"}


async def _ensure_ollama_backend(admin_client: AsyncClient) -> None:
    """Plant unified ollama creds via the real PATCH so ``to_llm_config()`` resolves."""
    resp = await admin_client.patch(
        "/admin/platform-config",
        json={"llm_backend": "ollama", "llm_base_url": "http://localhost:11434"},
    )
    assert resp.status_code == 200, resp.text


# ═══════════════════════════════════════════════════════════════════════════════
# Fakes + helpers (minimal subset of the test_enrichment_toggles.py pattern)
# ═══════════════════════════════════════════════════════════════════════════════


def _canned_payload(*, fact_object: str = "Tea") -> dict[str, Any]:
    """One full LLM output: classification + entity + fact, no invalidations."""
    return {
        "classification": {
            "intent": "greeting",
            "emotion": "joy",
            "valence": "positive",
            "arousal": "low",
            "confidence": 0.9,
        },
        "entities": [{"name": "Alice", "type": "Person"}],
        "relationships": [],
        "facts": [
            {
                "subject": "Alice",
                "predicate": "likes",
                "object": fact_object,
                "confidence": 0.9,
            }
        ],
        "structured_extractions": {},
        "invalidations": [],
    }


class _FakeLLM:
    """Stub backend: canned payloads, usage scales with prompt length."""

    def __init__(
        self,
        payloads: list[dict[str, Any]],
        sinks: list[Any],
    ) -> None:
        self._payloads = list(payloads)
        self._sinks = sinks

    async def chat(self, messages: list[dict], **kwargs: Any) -> ChatResponse:
        prompt = messages[-1]["content"] if messages else ""
        payload = self._payloads.pop(0) if self._payloads else _canned_payload()
        usage = TokenUsage(prompt_tokens=len(prompt), completion_tokens=10)
        response = ChatResponse(
            content=json.dumps(payload),
            model="fake",
            usage=usage,
            validated_data=CombinedLLMOutput.model_validate(payload),
        )
        if kwargs.get("metered", False) and self._sinks:
            await self._sinks[-1](
                UsageRecord(
                    provider="fake",
                    model="fake",
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    idempotency_key=uuid.uuid4().hex,
                )
            )
        return response


async def _create_session(
    auth_client: AsyncClient, project_id: UUID, external_id: str
) -> UUID:
    """Create a session; return its internal UUID."""
    resp = await auth_client.post(
        f"/v1/projects/{project_id}/sessions",
        json={"external_id": external_id},
    )
    assert resp.status_code == 201, resp.text
    return UUID(resp.json()["id"])


async def _ingest_one(
    auth_client: AsyncClient, project_id: UUID, session_external: str
) -> Any:
    """Ingest one user+assistant turn; assert 202."""
    payload = {
        "session_id": session_external,
        "messages": [
            {"role": "user", "content": "I love tea, especially oolong"},
            {"role": "assistant", "content": "Oolong is wonderful"},
        ],
    }
    resp = await auth_client.post(
        f"/v1/projects/{project_id}/memory",
        data={"data": json.dumps(payload)},
    )
    assert resp.status_code == 202, resp.text
    return resp


async def _episode_ids_for_session(engine: Any, session_id: UUID) -> list[UUID]:
    async with AsyncSession(engine) as db:
        rows = (
            (
                await db.execute(
                    select(Episode.id)
                    .where(
                        Episode.session_id == session_id,
                        Episode.is_deleted.is_(False),
                    )
                    .order_by(Episode.sequence_number)
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


async def _run_enrich(
    isolated_app: Any,
    redis_client: Any,
    falkordb_url: str,
    *,
    episode_ids: list[UUID],
    org_id: UUID,
    project_id: UUID,
    session_id: UUID,
    enrichment_gate: dict | None,
    payloads: list[dict[str, Any]] | None = None,
) -> AsyncMock:
    """Run the real ``enrich_episode`` per episode; return the LLM mock.

    Graph resolution stays real (FalkorDB testcontainer + prod
    dispatcher); the LLM backend is stubbed with canned payloads.
    """
    from workers.tasks.enrich_episode import enrich_episode

    from core.graph_backend import init_dispatcher
    from falkordb.asyncio import FalkorDB
    from redis.asyncio import BlockingConnectionPool
    from services import usage_service as usage_module

    created_sinks: list[Any] = []
    real_make_sink = usage_module.make_sink

    def _spy_sink(target: Any, **kwargs: Any) -> Any:
        sink = real_make_sink(target, **kwargs)
        created_sinks.append(sink)
        return sink

    fake = _FakeLLM(payloads or [], created_sinks)
    llm = AsyncMock()
    llm.chat.side_effect = fake.chat
    pool = BlockingConnectionPool.from_url(
        falkordb_url, max_connections=5, decode_responses=True
    )
    client = FalkorDB(connection_pool=pool)
    ctx = {
        "db_session_factory": isolated_app.state.db_session_factory,
        "redis": redis_client,
        "graph_backend_dispatcher": init_dispatcher(),
        "falkordb_client": client,
    }
    try:
        with (
            patch("core.llm.resolve_backend", return_value=llm),
            patch.object(usage_module, "make_sink", side_effect=_spy_sink),
        ):
            for episode_id in episode_ids:
                await enrich_episode(
                    ctx=ctx,
                    episode_id=str(episode_id),
                    user_id=str(org_id),
                    project_id=str(project_id),
                    session_id=str(session_id),
                    enrichment_gate=enrichment_gate,
                )
    finally:
        await client.connection.aclose()
    return llm


async def _episode_row(engine: Any, episode_id: UUID) -> Episode:
    async with AsyncSession(engine) as db:
        row = (
            await db.execute(select(Episode).where(Episode.id == episode_id))
        ).scalar_one()
        db.expunge(row)
        return row


async def _fact_count(engine: Any, org_id: UUID) -> int:
    async with AsyncSession(engine) as db:
        return await db.scalar(
            select(func.count(Fact.id)).where(Fact.user_id == org_id)
        )


# ═══════════════════════════════════════════════════════════════════════════════
# (8) Gate ON + low-signal → mark-skipped bits, no LLM call
# ═══════════════════════════════════════════════════════════════════════════════


class TestGateSkipsLowSignal:
    async def test_low_signal_stamps_llm_bits_without_llm_call(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
        isolated_auth_client: AsyncClient,
        redis_client: Any,
        falkordb_url: str,
        engine: Any,
    ) -> None:
        """Gate skip → all five LLM bits set, ``chat`` never awaited, no
        facts persisted, episode content and (non-LLM) embedding state
        untouched — embed/link travel in separate ARQ payloads and are
        unaffected by the gate decision."""
        org_id: UUID = isolated_org_and_key["user_id"]
        project_id: UUID = isolated_org_and_key["project_id"]

        session_id = await _create_session(
            isolated_auth_client, project_id, "gate-skip"
        )
        await _ingest_one(isolated_auth_client, project_id, "gate-skip")
        episode_ids = await _episode_ids_for_session(engine, session_id)
        assert len(episode_ids) == 2

        with patch(
            "core.enrichment_gate.should_skip",
            new=AsyncMock(return_value=(True, 0.04)),
        ):
            llm = await _run_enrich(
                isolated_app,
                redis_client,
                falkordb_url,
                episode_ids=episode_ids,
                org_id=org_id,
                project_id=project_id,
                session_id=session_id,
                enrichment_gate=dict(_GATE_ON),
                payloads=[_canned_payload(fact_object=f"Tea-{i}") for i in range(2)],
            )

        llm.chat.assert_not_awaited()
        for episode_id in episode_ids:
            row = await _episode_row(engine, episode_id)
            assert row.enrichment_status & _LLM_BITS == _LLM_BITS
            assert row.content != ""
            assert row.embedding is None
        assert await _fact_count(engine, org_id) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# (9) Gate ON + high-signal → full LLM path
# ═══════════════════════════════════════════════════════════════════════════════


class TestGateRunsHighSignal:
    async def test_high_signal_runs_full_llm_path(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
        isolated_auth_client: AsyncClient,
        isolated_admin_client: AsyncClient,
        redis_client: Any,
        falkordb_url: str,
        engine: Any,
    ) -> None:
        """Gate pass → ``chat`` awaited once per episode, facts land, all
        five LLM bits set via the normal (non-skip) path."""
        org_id: UUID = isolated_org_and_key["user_id"]
        project_id: UUID = isolated_org_and_key["project_id"]
        await _ensure_ollama_backend(isolated_admin_client)

        session_id = await _create_session(
            isolated_auth_client, project_id, "gate-run"
        )
        await _ingest_one(isolated_auth_client, project_id, "gate-run")
        episode_ids = await _episode_ids_for_session(engine, session_id)
        assert len(episode_ids) == 2

        with patch(
            "core.enrichment_gate.should_skip",
            new=AsyncMock(return_value=(False, 0.96)),
        ):
            llm = await _run_enrich(
                isolated_app,
                redis_client,
                falkordb_url,
                episode_ids=episode_ids,
                org_id=org_id,
                project_id=project_id,
                session_id=session_id,
                enrichment_gate=dict(_GATE_ON),
                payloads=[_canned_payload(fact_object=f"Tea-{i}") for i in range(2)],
            )

        assert llm.chat.await_count == len(episode_ids)
        assert await _fact_count(engine, org_id) == len(episode_ids)
        for episode_id in episode_ids:
            row = await _episode_row(engine, episode_id)
            assert row.enrichment_status & _LLM_BITS == _LLM_BITS


# ═══════════════════════════════════════════════════════════════════════════════
# (10) Gate ON + broken model → fail-open full path, no retry loop
# ═══════════════════════════════════════════════════════════════════════════════


class TestGateBrokenModelFailOpen:
    async def test_load_failure_enriches_normally(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
        isolated_auth_client: AsyncClient,
        isolated_admin_client: AsyncClient,
        redis_client: Any,
        falkordb_url: str,
        engine: Any,
    ) -> None:
        """The real ``should_skip`` with a raising ``_load_pair`` fails open
        → full LLM path, exactly one ``chat`` per episode, no exception
        (hence no ARQ retry loop from the gate)."""
        org_id: UUID = isolated_org_and_key["user_id"]
        project_id: UUID = isolated_org_and_key["project_id"]
        await _ensure_ollama_backend(isolated_admin_client)

        session_id = await _create_session(
            isolated_auth_client, project_id, "gate-failopen"
        )
        await _ingest_one(isolated_auth_client, project_id, "gate-failopen")
        episode_ids = await _episode_ids_for_session(engine, session_id)
        assert len(episode_ids) == 2

        def _broken_load(model_name: str, token: Any | None) -> Any:
            raise OSError(f"no such model: {model_name}")

        with patch(
            "core.enrichment_gate._load_pair", side_effect=_broken_load
        ):
            llm = await _run_enrich(
                isolated_app,
                redis_client,
                falkordb_url,
                episode_ids=episode_ids,
                org_id=org_id,
                project_id=project_id,
                session_id=session_id,
                enrichment_gate={
                    "enabled": True,
                    "threshold": 0.99,
                    "model": "bogus/gate-broken-xyz",
                },
                payloads=[_canned_payload(fact_object=f"Tea-{i}") for i in range(2)],
            )

        # Even at threshold 0.99 the fail-open (False, 0.0) enriches.
        assert llm.chat.await_count == len(episode_ids)
        assert await _fact_count(engine, org_id) == len(episode_ids)


# ═══════════════════════════════════════════════════════════════════════════════
# (11) PATCH /admin/system/config enable-path (real HTTP, admin JWT)
# ═══════════════════════════════════════════════════════════════════════════════


def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bomb ``urlopen`` — any real Hub request fails the test loudly."""

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("network access attempted in offline test")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)


@pytest_asyncio.fixture(loop_scope="function")
async def _superadmin_client(isolated_app: Any) -> Any:
    """HTTP client authed as the platform admin (setup → login)."""
    transport = asgi_transport(isolated_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        setup = await client.post(
            "/v1/auth/setup", json={"password": "StrongPass1"}  # noqa: S106
        )
        assert setup.status_code == 201, setup.text
        login = await client.post(
            "/v1/auth/login",
            json={"email": "root", "password": "StrongPass1"},  # noqa: S106
        )
        assert login.status_code == 200, login.text
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        yield client


# ═══════════════════════════════════════════════════════════════════════════════
# (12) GET /admin/platform-config with legacy dict-shaped stored gate → coerced bool
# ═══════════════════════════════════════════════════════════════════════════════


async def _plant_legacy_gate(engine: Any, user_id: UUID, raw: dict) -> None:
    """Write a poisoned legacy SYSTEM-block dict straight into the DB row."""
    async with AsyncSession(engine) as db:
        await db.execute(
            sql_text(
                "UPDATE users SET config_override = CAST(:cfg AS JSONB) "
                "WHERE id = :uid"
            ),
            {"cfg": json.dumps({"enrichment_gate": raw}), "uid": user_id},
        )
        await db.commit()


class TestLegacyOrgConfigRegression:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [({"enabled": True}, True), ({"enabled": False}, False), ({}, False)],
    )
    async def test_legacy_dict_shape_coerced_on_read(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
        isolated_admin_client: AsyncClient,
        engine: Any,
        raw: dict,
        expected: bool,
    ) -> None:
        """A poisoned-row legacy dict reads back 200 with a coerced gate
        object — the GET never 500s/422s on the old shape."""
        await _plant_legacy_gate(engine, isolated_org_and_key["user_id"], raw)

        resp = await isolated_admin_client.get("/admin/platform-config")
        assert resp.status_code == 200, resp.text
        stored = resp.json()["stored"]
        assert stored["enrichment_gate"] == {
            "enabled": expected,
            "threshold": 0.5,
        }
