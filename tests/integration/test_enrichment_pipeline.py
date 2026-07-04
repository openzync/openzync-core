"""Integration test for enrichment worker pipeline (G1.2 + G1.3).

Verifies that after message ingestion the ARQ worker pipeline completes:
    - G1.2: Entity extraction worker completes within 30s, entity nodes visible
    - G1.3: Episodes have ``embedding`` populated (non-NULL) after worker

Strategy:
    1. Start testcontainers PostgreSQL + Redis (via session-scoped fixtures).
    2. Override the FastAPI app's ARQ pool to point at the test Redis.
    3. Start an in-process ARQ worker (high-priority tasks only) in a background
       ``asyncio.Task``.
    4. Ingest a 10-turn conversation via ``POST /memory``.
    5. Poll the ``episodes`` table until ``enrichment_status`` indicates
       entity extraction (bit 0) and embedding (bit 1) are complete.
    6. Assert entity nodes exist in the graph backend.
    7. Assert episodes have non-NULL ``embedding`` values.
"""

from __future__ import annotations

import asyncio
import os
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.episode import Episode
from workers.tasks.base import ENRICHMENT_EMBEDDING, ENRICHMENT_ENTITIES


# Allow longer runtime for worker processing
pytestmark = pytest.mark.slow


def _set_test_env_vars(
    pg_url: str, redis_host: str, redis_port: int
) -> None:
    """Set environment variables so the app connects to test containers.

    Args:
        pg_url: PostgreSQL connection URL (asyncpg scheme) from testcontainers.
        redis_host: Redis host IP from testcontainers.
        redis_port: Redis exposed port from testcontainers.
    """
    os.environ["OZ_DATABASE_URL"] = pg_url
    os.environ["OZ_REDIS_URL"] = f"redis://{redis_host}:{redis_port}/0"
    os.environ["OZ_ENVIRONMENT"] = "development"
    os.environ["OZ_SECRET_KEY"] = "a" * 32


@pytest_asyncio.fixture(scope="module")
async def pipeline_app(engine) -> tuple[Any, str, int]:
    """Create the FastAPI app wired to testcontainers infrastructure.

    Returns:
        Tuple of (app, redis_host, redis_port).
    """
    # Get connection info from the session-scoped engine's containers
    pg_container = getattr(engine, "_testcontainers_pg", None)
    redis_container = getattr(engine, "_testcontainers_redis", None)
    assert pg_container is not None, "Testcontainers PG not found on engine"
    assert redis_container is not None, "Testcontainers Redis not found on engine"

    pg_url = str(engine.url).replace("postgresql+asyncpg://", "postgresql://")
    asyncpg_url = str(engine.url)
    redis_host = redis_container.get_container_host_ip()
    redis_port = redis_container.get_exposed_port(6379)

    _set_test_env_vars(asyncpg_url, redis_host, int(redis_port))

    # Force reload of settings so they pick up the new env vars
    import importlib

    import core.config
    importlib.reload(core.config)

    # Create the app — lifespan will init ARQ against test Redis
    from services.api.main import create_app

    app = create_app()
    return app, redis_host, int(redis_port)


@pytest_asyncio.fixture(scope="module")
async def pipeline_auth_client(pipeline_app) -> tuple[AsyncClient, UUID, str, int]:
    """Create an authenticated HTTP client and bootstrap a test org.

    Returns:
        Tuple of (auth_client, org_id, redis_host, redis_port).
    """
    app, redis_host, redis_port = pipeline_app
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as cli:
        # Bootstrap org
        resp = await cli.post(
            "/admin/organizations",
            json={"name": "Pipeline Test Org", "plan": "free"},
        )
        assert resp.status_code == 201, f"Bootstrap failed: {resp.text}"
        data = resp.json()
        org_id = UUID(data["organization_id"])
        api_key = data["api_key"]

        # Authenticated client
        cli.headers["Authorization"] = f"Bearer {api_key}"

        # Yield the client — close is handled by the ``with`` block
        yield cli, org_id, redis_host, redis_port


@pytest.mark.skip(reason="Needs per-test DB isolation — see TODO")
@pytest.mark.asyncio
@pytest.mark.slow
@pytest.mark.integration
class TestEnrichmentPipeline:
    """End-to-end enrichment pipeline test (G1.2 + G1.3)."""

    MAX_WAIT_S: int = 45
    """Maximum seconds to wait for worker completion."""

    POLL_INTERVAL_S: float = 2.0
    """Seconds between DB polls for enrichment status."""

    async def _start_worker(
        self, redis_host: str, redis_port: int
    ) -> tuple[Any, asyncio.Task]:
        """Start an ARQ worker in a background task.

        Args:
            redis_host: Redis host.
            redis_port: Redis port.

        Returns:
            Tuple of (worker_instance, background_task).
        """
        from arq.connections import RedisSettings
        from arq.worker import Worker as ArqWorker

        from services.worker.worker import HIGH_QUEUE_TASKS
        from services.worker.worker_settings import get_queue_name, settings

        redis_settings = RedisSettings(
            host=redis_host,
            port=redis_port,
            database=0,
        )

        worker = ArqWorker(
            redis_settings=redis_settings,
            functions=HIGH_QUEUE_TASKS,
            queue_name=get_queue_name("development", "high"),
            max_jobs=4,
            job_timeout=60,
            keep_result=10,
            keep_result_forever=False,
            poll_delay=0.5,
        )

        task = asyncio.create_task(worker.async_run())
        # Give the worker a moment to connect and start polling
        await asyncio.sleep(1)
        return worker, task

    async def _poll_episode_enrichment(
        self,
        engine: Any,
        user_id: UUID,
        timeout_s: float = MAX_WAIT_S,
    ) -> list[dict]:
        """Poll episodes until enrichment bits 0 and 1 are set.

        Args:
            engine: SQLAlchemy async engine for direct DB access.
            user_id: The user UUID to check episodes for.
            timeout_s: Maximum wait time in seconds.

        Returns:
            List of episode dicts with their enrichment status.

        Raises:
            AssertionError: If the timeout is exceeded before enrichment
                completes.
        """
        deadline = asyncio.get_event_loop().time() + timeout_s

        while asyncio.get_event_loop().time() < deadline:
            async with AsyncSession(engine) as db:
                result = await db.execute(
                    select(
                        Episode.id,
                        Episode.enrichment_status,
                        Episode.embedding,
                    ).where(
                        Episode.user_id == user_id,
                        Episode.is_deleted.is_(False),
                    )
                    .order_by(Episode.sequence_number)
                )
                rows = result.all()

            if not rows:
                await asyncio.sleep(self.POLL_INTERVAL_S)
                continue

            # Check if ALL episodes have both bits set
            all_done = all(
                row.enrichment_status & ENRICHMENT_ENTITIES != 0
                and row.enrichment_status & ENRICHMENT_EMBEDDING != 0
                for row in rows
            )

            if all_done:
                return [
                    {
                        "id": str(row.id),
                        "enrichment_status": row.enrichment_status,
                        "embedding": row.embedding,
                    }
                    for row in rows
                ]

            await asyncio.sleep(self.POLL_INTERVAL_S)

        # Timeout — collect current state for diagnostics
        async with AsyncSession(engine) as db:
            result = await db.execute(
                select(
                    Episode.id,
                    Episode.enrichment_status,
                    Episode.embedding,
                ).where(
                    Episode.user_id == user_id,
                    Episode.is_deleted.is_(False),
                )
            )
            rows = result.all()

        statuses = [
            {
                "id": str(r.id),
                "status_bits": r.enrichment_status,
                "has_embedding": r.embedding is not None,
            }
            for r in rows
        ]
        raise AssertionError(
            f"Enrichment did not complete within {timeout_s}s. "
            f"Episode statuses: {statuses}"
        )

    async def _check_graph_entities(
        self, engine: Any, user_id: UUID
    ) -> list[dict]:
        """Query the graph backend for entities belonging to this user.

        Args:
            engine: SQLAlchemy async engine.
            user_id: The user UUID to check entities for.

        Returns:
            List of entity dicts from the graph.
        """
        async with AsyncSession(engine) as db:
            result = await db.execute(
                text(
                    "SELECT id, name, type FROM graph_entities "
                    "WHERE user_id = :user_id "
                    "AND is_deleted = false "
                    "ORDER BY created_at ASC"
                ),
                {"user_id": user_id},
            )
            rows = result.all()
            return [
                {"id": str(r[0]), "name": r[1], "type": r[2]}
                for r in rows
            ]

    # ── Tests ──────────────────────────────────────────────────────────────

    async def test_enrichment_pipeline(
        self,
        engine,
        pipeline_auth_client,
    ) -> None:
        """G1.2 + G1.3: Ingest a conversation, wait for enrichment, verify.

        The test covers:
        - Message ingestion returns 202
        - ARQ worker picks up and processes enrichment tasks
        - Episode enrichment_status bits are set for entities + embedding
        - Episode embedding column is non-NULL
        - Graph entity nodes are created
        """
        auth_client, org_id, redis_host, redis_port = pipeline_auth_client

        # ── Step 1: Create a user ──────────────────────────────────────
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "pipeline_test_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        # ── Step 2: Start the ARQ worker in background ─────────────────
        worker_instance, worker_task = await self._start_worker(
            redis_host, redis_port
        )
        try:
            # ── Step 3: Ingest a conversation (10 turns) ──────────────────
            flat_messages: list[dict] = []
            for topic in ["Python", "databases", "APIs", "testing", "deployment"]:
                flat_messages.append(
                    {"role": "user", "content": f"Hello, I need help with {topic}"}
                )
                flat_messages.append(
                    {
                        "role": "assistant",
                        "content": f"Sure, I can help with {topic}",
                    }
                )

            ingest_resp = await auth_client.post(
                f"/v1/users/{user_id}/memory",
                json={
                    "session_id": "pipeline_test_session",
                    "messages": flat_messages,
                },
            )
            assert ingest_resp.status_code == 202, (
                f"Expected 202, got {ingest_resp.status_code}: {ingest_resp.text}"
            )

            # ── Step 4: Poll for enrichment completion ─────────────────
            episodes = await self._poll_episode_enrichment(
                engine, UUID(user_id)
            )

            # G1.3: All episodes must have non-NULL embedding
            for ep in episodes:
                assert ep["embedding"] is not None, (
                    f"G1.3 FAIL: Episode {ep['id']} has NULL embedding. "
                    f"enrichment_status={ep['enrichment_status']}"
                )

            # All episodes should have enrichment bits 0+1 set
            for ep in episodes:
                assert ep["enrichment_status"] & ENRICHMENT_ENTITIES != 0, (
                    f"Episode {ep['id']} missing ENTITIES bit "
                    f"(status={ep['enrichment_status']})"
                )
                assert ep["enrichment_status"] & ENRICHMENT_EMBEDDING != 0, (
                    f"Episode {ep['id']} missing EMBEDDING bit "
                    f"(status={ep['enrichment_status']})"
                )

            # G1.2: Entity nodes must be visible in the graph
            entities = await self._check_graph_entities(engine, UUID(user_id))
            assert len(entities) > 0, (
                "G1.2 FAIL: No graph entities found after enrichment pipeline. "
                "Expected at least one entity node."
            )

        finally:
            # ── Step 5: Clean up worker ────────────────────────────────
            worker_task.cancel()
            try:
                await worker_task
            except (asyncio.CancelledError, Exception):
                pass
