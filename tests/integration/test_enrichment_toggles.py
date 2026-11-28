"""Integration tests for per-episode enrichment toggles (live PG15+Redis).

Covers the user-dictated list (31–37) end to end: real HTTP ingest against
the isolated app, then the real ``enrich_episode`` worker driven directly
per episode.

Test-doubles boundary: the LLM backend is stubbed (``core.llm.resolve_backend``
patched) with a fake whose ``usage.prompt_tokens`` equals the rendered
prompt length — there are no LLM API keys in test.  Everything else is
real: Postgres rows, FalkorDB graph writes, Redis, RLS tenant binding,
org-config resolution, the Jinja template, and the ``llm_usage`` metering
write path.  Token assertions therefore check *direction only* (disabled
⇒ shorter prompt ⇒ fewer tokens), never exact counts.

"Pending" below means episodes whose five ``enrich_episode`` LLM bits
(entities | facts | classification | structured_extraction |
llm_invalidation) are incomplete.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.llm import ChatResponse, TokenUsage, UsageRecord
from models.episode import Episode
from models.fact import Fact
from models.llm_usage import LLMUsage
from schemas.llm_outputs import CombinedLLMOutput
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


# ═══════════════════════════════════════════════════════════════════════════════
# Fakes + helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _canned_payload(
    *,
    classification: bool = True,
    entities: bool = True,
    facts: bool = True,
    fact_object: str = "Tea",
) -> dict[str, Any]:
    """Canned LLM output matching the test's toggle set.

    Sections disabled in a given test are emitted empty so the fake never
    contradicts the toggle contract (the real validator is bypassed —
    it is covered by unit tests).  ``fact_object`` keeps each episode's
    fact distinct so cross-episode dedup never collapses the count.
    """
    return {
        "classification": (
            {
                "intent": "greeting",
                "emotion": "joy",
                "valence": "positive",
                "arousal": "low",
                "confidence": 0.9,
            }
            if classification
            else {}
        ),
        "entities": (
            [{"name": "Alice", "type": "Person"}] if entities else []
        ),
        "relationships": [],
        "facts": (
            [
                {
                    "subject": "Alice",
                    "predicate": "likes",
                    "object": fact_object,
                    "confidence": 0.9,
                }
            ]
            if facts
            else []
        ),
        "structured_extractions": {},
        "invalidations": [],
    }


class _FakeLLM:
    """Stub backend: one canned payload per episode, usage scales with prompt.

    ``prompt_tokens = len(prompt)`` keeps token assertions to direction
    only — shorter prompt (fewer sections) ⇒ strictly fewer tokens.
    Usage is emitted through the *real* metering sink captured from
    ``make_sink`` (the stubbed ``resolve_backend`` would otherwise drop
    it), so the ``llm_usage`` write path stays fully live.
    """

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


def _messages(n: int = 2) -> list[dict[str, str]]:
    """Small deterministic conversation about tea."""
    out: list[dict[str, str]] = []
    for i in range(n):
        out.append({"role": "user", "content": f"Hello {i}, I love tea"})
        out.append(
            {"role": "assistant", "content": f"Hi {i}, tea is wonderful"}
        )
    return out


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


async def _ingest(
    auth_client: AsyncClient,
    project_id: UUID,
    session_external: str,
    messages: list[dict[str, str]],
    enrichments: dict[str, bool] | None = None,
) -> Any:
    """Ingest messages; ``enrichments`` omitted when ``None``."""
    payload: dict[str, Any] = {
        "session_id": session_external,
        "messages": messages,
    }
    if enrichments is not None:
        payload["enrichments"] = enrichments
    return await auth_client.post(
        f"/v1/projects/{project_id}/memory",
        data={"data": json.dumps(payload)},
    )


async def _run_worker(
    isolated_app: Any,
    redis_client: Any,
    falkordb_url: str,
    *,
    episode_ids: list[UUID],
    org_id: UUID,
    project_id: UUID,
    session_id: UUID,
    payloads: list[dict[str, Any]],
    enrichments: dict[str, bool] | None = None,
) -> None:
    """Run the real ``enrich_episode`` for each episode (stubbed LLM).

    Graph resolution is real: a FalkorDB client for the testcontainer
    plus the prod dispatcher go into the worker ctx, so entity
    persistence (bit 0) works exactly as in production.
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

    fake = _FakeLLM(payloads, created_sinks)
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
                    enrichments=enrichments,
                )
    finally:
        await client.connection.aclose()


async def _episode_ids_for_session(
    engine: Any, session_id: UUID
) -> list[UUID]:
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


async def _pending_count(engine: Any, org_id: UUID) -> int:
    """Episodes whose five LLM bits are incomplete."""
    async with AsyncSession(engine) as db:
        statuses = (
            (
                await db.execute(
                    select(Episode.enrichment_status).where(
                        Episode.user_id == org_id,
                        Episode.is_deleted.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )
    return sum(1 for s in statuses if (s & _LLM_BITS) != _LLM_BITS)


async def _fact_count(engine: Any, org_id: UUID) -> int:
    async with AsyncSession(engine) as db:
        return await db.scalar(
            select(func.count(Fact.id)).where(Fact.user_id == org_id)
        )


async def _session_fact_count(
    engine: Any, episode_ids: list[UUID]
) -> int:
    async with AsyncSession(engine) as db:
        return await db.scalar(
            select(func.count(Fact.id)).where(
                Fact.source_episode_id.in_(episode_ids)
            )
        )


async def _classification_count(engine: Any, org_id: UUID) -> int:
    from sqlalchemy import text

    async with AsyncSession(engine) as db:
        return await db.scalar(
            text("SELECT COUNT(*) FROM dialog_classifications WHERE user_id = :u"),
            {"u": org_id},
        )


async def _usage_tokens(engine: Any, org_id: UUID) -> list[int]:
    async with AsyncSession(engine) as db:
        rows = (
            (
                await db.execute(
                    select(LLMUsage.prompt_tokens).where(
                        LLMUsage.user_id == org_id,
                        LLMUsage.worker == "enrich_episode",
                    )
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


async def _patch_org(admin_client: AsyncClient, payload: dict[str, Any]) -> None:
    resp = await admin_client.patch("/admin/platform-config", json=payload)
    assert resp.status_code == 200, resp.text


# ═══════════════════════════════════════════════════════════════════════════════
# (31) all-on fresh ingest → pending 0, all sections, llm_usage tokens
# ═══════════════════════════════════════════════════════════════════════════════


class TestAllOnIngest:
    async def test_all_on_completes_with_sections_and_usage(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
        isolated_auth_client: AsyncClient,
        isolated_admin_client: AsyncClient,
        redis_client: Any,
        falkordb_url: str,
        engine: Any,
    ) -> None:
        """(31) All-on ingest: pending → 0, facts + classifications land,
        and every episode emits an ``llm_usage`` row with prompt tokens."""
        org_id: UUID = isolated_org_and_key["user_id"]
        project_id: UUID = isolated_org_and_key["project_id"]
        await _patch_org(
            isolated_admin_client,
            {"llm_backend": "ollama", "llm_base_url": "http://localhost:11434"},
        )

        session_id = await _create_session(
            isolated_auth_client, project_id, "all-on-session"
        )
        resp = await _ingest(
            isolated_auth_client, project_id, "all-on-session", _messages(2)
        )
        assert resp.status_code == 202, resp.text

        episode_ids = await _episode_ids_for_session(engine, session_id)
        assert len(episode_ids) == 4

        await _run_worker(
            isolated_app,
            redis_client,
            falkordb_url,
            episode_ids=episode_ids,
            org_id=org_id,
            project_id=project_id,
            session_id=session_id,
            payloads=[
                _canned_payload(fact_object=f"Tea-{i}")
                for i in range(len(episode_ids))
            ],
        )

        assert await _pending_count(engine, org_id) == 0
        assert await _fact_count(engine, org_id) == 4
        assert await _classification_count(engine, org_id) == 4
        tokens = await _usage_tokens(engine, org_id)
        assert len(tokens) == 4
        assert all(t > 0 for t in tokens)


# ═══════════════════════════════════════════════════════════════════════════════
# (32) facts=False → zero facts, prompt tokens strictly lower than all-on
# ═══════════════════════════════════════════════════════════════════════════════


class TestFactsOffUsesFewerTokens:
    async def test_facts_off_zero_facts_and_lower_tokens(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
        isolated_auth_client: AsyncClient,
        isolated_admin_client: AsyncClient,
        redis_client: Any,
        falkordb_url: str,
        engine: Any,
    ) -> None:
        """(32) Same org, two sessions: all-on baseline vs ``facts=False``.

        The comparison is self-contained (no cross-test coupling): the
        facts-off prompt omits the facts OUTPUT SCHEMA block and the facts
        DB providers, so its prompt is strictly shorter — direction only,
        never exact counts.
        """
        org_id: UUID = isolated_org_and_key["user_id"]
        project_id: UUID = isolated_org_and_key["project_id"]

        # Unified ollama creds so the worker's ``to_llm_config()`` resolves.
        await _patch_org(
            isolated_admin_client,
            {"llm_backend": "ollama", "llm_base_url": "http://localhost:11434"},
        )

        # Baseline: all-on.
        base_session = await _create_session(
            isolated_auth_client, project_id, "tok-base"
        )
        resp = await _ingest(
            isolated_auth_client, project_id, "tok-base", _messages(1)
        )
        assert resp.status_code == 202, resp.text

        # Variant: facts off per-request (invalidation off too — it
        # requires facts, so the combo stays valid).
        off_session = await _create_session(
            isolated_auth_client, project_id, "tok-off"
        )
        resp = await _ingest(
            isolated_auth_client,
            project_id,
            "tok-off",
            _messages(1),
            enrichments={"facts": False, "llm_invalidation": False},
        )
        assert resp.status_code == 202, resp.text

        base_ids = await _episode_ids_for_session(engine, base_session)
        off_ids = await _episode_ids_for_session(engine, off_session)

        await _run_worker(
            isolated_app,
            redis_client,
            falkordb_url,
            episode_ids=base_ids,
            org_id=org_id,
            project_id=project_id,
            session_id=base_session,
            payloads=[
                _canned_payload(fact_object=f"Base-{i}")
                for i in range(len(base_ids))
            ],
        )
        assert await _session_fact_count(engine, base_ids) == 2

        await _run_worker(
            isolated_app,
            redis_client,
            falkordb_url,
            episode_ids=off_ids,
            org_id=org_id,
            project_id=project_id,
            session_id=off_session,
            payloads=[_canned_payload(facts=False) for _ in off_ids],
            enrichments={"facts": False, "llm_invalidation": False},
        )
        assert await _session_fact_count(engine, off_ids) == 0

        tokens = await _usage_tokens(engine, org_id)
        assert len(tokens) == 4
        base_tokens = sorted(tokens)[-2:]
        off_tokens = sorted(tokens)[:2]
        assert sum(off_tokens) < sum(base_tokens)


# ═══════════════════════════════════════════════════════════════════════════════
# (33) facts-without-entities → live 422
# ═══════════════════════════════════════════════════════════════════════════════


class TestInvalidCombo422:
    async def test_facts_without_entities_live_422(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
        isolated_auth_client: AsyncClient,
        engine: Any,
    ) -> None:
        """(33) ``facts=True`` + ``entities=False`` is rejected at ingest."""
        org_id: UUID = isolated_org_and_key["user_id"]
        project_id: UUID = isolated_org_and_key["project_id"]

        await _create_session(isolated_auth_client, project_id, "bad-combo")

        resp = await _ingest(
            isolated_auth_client,
            project_id,
            "bad-combo",
            _messages(1),
            enrichments={"facts": True, "entities": False},
        )
        assert resp.status_code == 422, resp.text

        async with AsyncSession(engine) as db:
            count = await db.scalar(
                select(func.count(Episode.id)).where(
                    Episode.user_id == org_id,
                    Episode.is_deleted.is_(False),
                )
            )
        assert count == 0


# ═══════════════════════════════════════════════════════════════════════════════
# (34) stored facts=False suppresses; per-request facts=True overrides
# ═══════════════════════════════════════════════════════════════════════════════


class TestStoredSuppressedRequestOverrides:
    async def test_stored_off_suppresses_request_on_overrides(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
        isolated_auth_client: AsyncClient,
        isolated_admin_client: AsyncClient,
        redis_client: Any,
        falkordb_url: str,
        engine: Any,
    ) -> None:
        """(34) Stored ``facts=False`` wins over the default; an explicit
        per-request ``facts=True`` wins over the stored override."""
        org_id: UUID = isolated_org_and_key["user_id"]
        project_id: UUID = isolated_org_and_key["project_id"]

        # Stored facts=False (invalidation off too — it requires facts),
        # plus unified ollama creds so the worker's ``to_llm_config()`` resolves.
        await _patch_org(
            isolated_admin_client,
            {
                "llm_backend": "ollama",
                "llm_base_url": "http://localhost:11434",
                "enrichment": {"facts": False, "llm_invalidation": False},
            },
        )

        # Stored-off ingest → no facts.
        stored_session = await _create_session(
            isolated_auth_client, project_id, "stored-off"
        )
        resp = await _ingest(
            isolated_auth_client, project_id, "stored-off", _messages(1)
        )
        assert resp.status_code == 202, resp.text
        stored_ids = await _episode_ids_for_session(engine, stored_session)
        await _run_worker(
            isolated_app,
            redis_client,
            falkordb_url,
            episode_ids=stored_ids,
            org_id=org_id,
            project_id=project_id,
            session_id=stored_session,
            payloads=[_canned_payload() for _ in stored_ids],
            # The stored-off effective map — exactly what ingest forwards
            # to ARQ (facts + its dependent invalidation suppressed).
            enrichments={"facts": False, "llm_invalidation": False},
        )
        assert await _session_fact_count(engine, stored_ids) == 0

        # Per-request override → facts flow.
        override_session = await _create_session(
            isolated_auth_client, project_id, "req-override"
        )
        resp = await _ingest(
            isolated_auth_client,
            project_id,
            "req-override",
            _messages(1),
            enrichments={"facts": True},
        )
        assert resp.status_code == 202, resp.text
        override_ids = await _episode_ids_for_session(engine, override_session)
        await _run_worker(
            isolated_app,
            redis_client,
            falkordb_url,
            episode_ids=override_ids,
            org_id=org_id,
            project_id=project_id,
            session_id=override_session,
            payloads=[
                _canned_payload(fact_object=f"Ovr-{i}")
                for i in range(len(override_ids))
            ],
            # Effective map for request {facts: True} over stored
            # {facts: False, llm_invalidation: False}: facts win back,
            # the stored invalidation opt-out still holds.
            enrichments={"facts": True, "llm_invalidation": False},
        )
        assert await _session_fact_count(engine, override_ids) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# (35) set → null-reset → read-back default → ingest uses defaults
# ═══════════════════════════════════════════════════════════════════════════════


class TestNullResetRestoresDefaults:
    async def test_set_null_reset_readback_ingest_defaults(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
        isolated_auth_client: AsyncClient,
        isolated_admin_client: AsyncClient,
        redis_client: Any,
        falkordb_url: str,
        engine: Any,
    ) -> None:
        """(35) Stored ``facts=False`` → ``null`` reset → read-back shows
        the default → a plain ingest enriches facts again."""
        org_id: UUID = isolated_org_and_key["user_id"]
        project_id: UUID = isolated_org_and_key["project_id"]

        await _patch_org(
            isolated_admin_client,
            {
                "llm_backend": "ollama",
                "llm_base_url": "http://localhost:11434",
                "enrichment": {"facts": False, "llm_invalidation": False},
            },
        )
        stored = (await isolated_admin_client.get("/admin/platform-config")).json()[
            "stored"
        ]
        assert stored["enrichment"]["facts"] is False

        await _patch_org(
            isolated_admin_client,
            {"enrichment": {"facts": None, "llm_invalidation": None}},
        )
        stored = (await isolated_admin_client.get("/admin/platform-config")).json()[
            "stored"
        ]
        assert "facts" not in (stored.get("enrichment") or {})
        assert "llm_invalidation" not in (stored.get("enrichment") or {})

        session_id = await _create_session(
            isolated_auth_client, project_id, "after-reset"
        )
        resp = await _ingest(
            isolated_auth_client, project_id, "after-reset", _messages(1)
        )
        assert resp.status_code == 202, resp.text
        episode_ids = await _episode_ids_for_session(engine, session_id)
        await _run_worker(
            isolated_app,
            redis_client,
            falkordb_url,
            episode_ids=episode_ids,
            org_id=org_id,
            project_id=project_id,
            session_id=session_id,
            payloads=[
                _canned_payload(fact_object=f"Rst-{i}")
                for i in range(len(episode_ids))
            ],
        )
        assert await _session_fact_count(engine, episode_ids) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# (36) embedding=False + hybrid search type → live 422
# ═══════════════════════════════════════════════════════════════════════════════


class TestEmbeddingHybrid422:
    async def test_embedding_off_with_hybrid_live_422(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
        isolated_auth_client: AsyncClient,
        isolated_admin_client: AsyncClient,
    ) -> None:
        """(36) ``embedding=False`` under ``graph_search_type=hybrid`` 422s —
        retrieval would silently lose its vector leg."""
        project_id: UUID = isolated_org_and_key["project_id"]

        await _patch_org(
            isolated_admin_client, {"graph_search_type": "hybrid"}
        )
        await _create_session(isolated_auth_client, project_id, "emb-hybrid")

        resp = await _ingest(
            isolated_auth_client,
            project_id,
            "emb-hybrid",
            _messages(1),
            enrichments={"embedding": False},
        )
        assert resp.status_code == 422, resp.text


# ═══════════════════════════════════════════════════════════════════════════════
# (37) classification=False → empty classifications, rest present
# ═══════════════════════════════════════════════════════════════════════════════


class TestClassificationOff:
    async def test_classification_off_empty_rest_present(
        self,
        isolated_app: Any,
        isolated_org_and_key: dict,
        isolated_auth_client: AsyncClient,
        isolated_admin_client: AsyncClient,
        redis_client: Any,
        falkordb_url: str,
        engine: Any,
    ) -> None:
        """(37) ``classification=False``: no classification rows, but facts
        land and the episode reads fully enriched (bit mark-skipped)."""
        org_id: UUID = isolated_org_and_key["user_id"]
        project_id: UUID = isolated_org_and_key["project_id"]
        await _patch_org(
            isolated_admin_client,
            {"llm_backend": "ollama", "llm_base_url": "http://localhost:11434"},
        )

        session_id = await _create_session(
            isolated_auth_client, project_id, "no-class"
        )
        resp = await _ingest(
            isolated_auth_client,
            project_id,
            "no-class",
            _messages(1),
            enrichments={"classification": False},
        )
        assert resp.status_code == 202, resp.text

        episode_ids = await _episode_ids_for_session(engine, session_id)
        await _run_worker(
            isolated_app,
            redis_client,
            falkordb_url,
            episode_ids=episode_ids,
            org_id=org_id,
            project_id=project_id,
            session_id=session_id,
            payloads=[
                _canned_payload(classification=False, fact_object=f"Cls-{i}")
                for i in range(len(episode_ids))
            ],
            enrichments={"classification": False},
        )

        assert await _classification_count(engine, org_id) == 0
        assert await _session_fact_count(engine, episode_ids) == 2
        assert await _pending_count(engine, org_id) == 0
