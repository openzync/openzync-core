"""Graphiti temporal knowledge-graph engine — connection lifecycle management.

Provides a module-level singleton pattern that integrates with FastAPI's
lifespan: call ``init_graphiti(...)`` on startup and ``close_graphiti()`` on
shutdown.  Access the ready instance via ``get_graphiti()``.

Graphiti's synchronous methods are wrapped with ``run_in_executor`` so they
never block the asyncio event loop.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from redis import Redis as RedisSync

try:
    from graphiti_core import Graphiti
    HAS_GRAPHITI = True
except ImportError:
    HAS_GRAPHITI = False

from core.config import settings
from core.exceptions import ExternalServiceError

logger = logging.getLogger(__name__)


class GraphitiClient:
    """Manages a single Graphiti temporal knowledge-graph engine instance.

    Responsibilities:
    * Creating and configuring the ``Graphiti`` object (sync SDK — calls are
      offloaded to a thread pool).
    * Verifying FalkorDB connectivity on startup via ``health_check``.
    * Graceful teardown on shutdown.

    Usage::

        client = GraphitiClient(falkordb_url=settings.FALKORDB_URL)
        await client.initialize()
        try:
            entity = await client.add_entity(...)
        finally:
            await client.close()
    """

    def __init__(
        self,
        falkordb_url: str | None = None,
        llm_client: Any | None = None,
        embedder: Any | None = None,
    ) -> None:
        """Initialise configuration; does **not** connect.

        Args:
            falkordb_url: Redis/FalkorDB connection string.  Falls back to
                ``settings.FALKORDB_URL``.
            llm_client: Optional LLM client for Graphiti's internal entity
                extraction.  If ``None``, Graphiti will not perform LLM-based
                enrichment.
            embedder: Optional embedding model for vector-similarity queries.
                If ``None``, Graphiti falls back to a default.
        """
        self._falkordb_url: str = falkordb_url or str(settings.FALKORDB_URL)
        self._llm_client: Any | None = llm_client
        self._embedder: Any | None = embedder
        self._graphiti: Graphiti | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Create the Graphiti SDK instance and verify backend connectivity.

        If ``graphiti-core`` is not installed, logs a warning and skips
        initialisation — the application runs without graph capabilities.

        Raises:
            ExternalServiceError: If the FalkorDB backend is unreachable or
                the configuration is invalid (only when graphiti-core IS installed).
        """
        if not HAS_GRAPHITI:
            logger.warning(
                "graphiti.init_skipped",
                extra={
                    "reason": "graphiti-core is not installed. "
                              "Graph-backed memory features will be unavailable. "
                              "Install with: pip install graphiti-core",
                },
            )
            return

        self._loop = asyncio.get_running_loop()

        try:
            # Build the FalkorDB driver and Graphiti instance.
            # Graphiti 0.29.1 defaults to Neo4jDriver — for FalkorDB we must
            # pass a FalkorDriver explicitly via the graph_driver parameter.
            self._graphiti = await self._loop.run_in_executor(
                None,
                lambda: self._build_graphiti(self._falkordb_url),
            )
        except Exception as exc:
            logger.error(
                "graphiti.initialization_failed",
                extra={"error": str(exc), "falkordb_url": self._falkordb_url},
            )
            raise ExternalServiceError(
                message=f"Failed to initialise Graphiti: {exc}",
                detail={"falkordb_url": self._falkordb_url},
            ) from exc

        # Verify the backend is actually reachable.
        healthy = await self.health_check()
        if not healthy:
            msg = "FalkorDB backend did not respond to PING"
            logger.error("graphiti.health_check_failed")
            raise ExternalServiceError(message=msg)

        logger.info("graphiti.initialized", extra={"falkordb_url": self._falkordb_url})

    @staticmethod
    def _build_graphiti(falkordb_url: str) -> object:
        """Create a Graphiti instance connected to FalkorDB.

        Since Graphiti 0.29.1 defaults to ``Neo4jDriver``, we explicitly
        create a ``FalkorDriver`` and pass it as ``graph_driver``.

        We also provide no-op implementations of ``LLMClient`` and
        ``EmbedderClient`` so Graphiti does not require an OpenAI API key
        to initialise.  OpenZep has its own entity-extraction and embedding
        pipelines (the ARQ enrichment workers), so Graphiti's built-in LLM
        calls are not needed.

        Args:
            falkordb_url: FalkorDB connection URL (redis://host:port).

        Returns:
            A configured ``graphiti_core.Graphiti`` instance.
        """
        if not HAS_GRAPHITI:
            raise RuntimeError(
                "graphiti-core is not installed. Install it with: pip install graphiti-core"
            )

        from urllib.parse import urlparse

        from graphiti_core.driver.falkordb_driver import FalkorDriver

        parsed = urlparse(falkordb_url)
        host = parsed.hostname or "localhost"
        port = parsed.port or 6379
        password = parsed.password

        falkordb_driver = FalkorDriver(
            host=host,
            port=port,
            password=password,
        )

        # Create no-op clients so Graphiti doesn't need an OpenAI API key.
        # OpenZep has its own entity-extraction, embedding, and reranker
        # pipelines — Graphiti's built-in LLM calls are not needed.
        from graphiti_core.llm_client.client import LLMClient as _LLMClient
        from graphiti_core.embedder.client import EmbedderClient as _EmbedderClient

        class _NoopLLMClient(_LLMClient):
            def __init__(self) -> None:
                super().__init__(config=None, cache=False)

            async def _generate_response(
                self,
                messages: list[dict],  # noqa: ARG002
                response_model: type | None = None,  # noqa: ARG002
                max_tokens: int = 1024,  # noqa: ARG002
                model_size: str = "medium",  # noqa: ARG002
            ) -> dict:
                return {}

        class _NoopEmbedderClient(_EmbedderClient):
            async def create(
                self, input_data: str | list[str] | list[int] | list[list[int]]  # noqa: ARG002
            ) -> list[float]:
                return []

        from graphiti_core.cross_encoder.client import CrossEncoderClient as _CrossEncoderClient

        class _NoopCrossEncoder(_CrossEncoderClient):
            """No-op cross-encoder — prevents Graphiti from requiring OpenAI keys."""

            async def rank(
                self, query: str, passages: list[str]  # noqa: ARG002
            ) -> list[tuple[str, float]]:
                return [(p, 0.0) for p in passages]

        noop_llm = _NoopLLMClient()
        noop_embedder = _NoopEmbedderClient()
        noop_cross_encoder = _NoopCrossEncoder()

        return Graphiti(
            graph_driver=falkordb_driver,
            llm_client=noop_llm,
            embedder=noop_embedder,
            cross_encoder=noop_cross_encoder,
        )

    async def close(self) -> None:
        """Release all Graphiti resources (connections, threads)."""
        if self._graphiti is not None:
            try:
                loop = self._loop or asyncio.get_running_loop()
                await loop.run_in_executor(None, self._graphiti.close)
            except Exception as exc:
                logger.warning("graphiti.close_error", extra={"error": str(exc)})
            finally:
                self._graphiti = None
                logger.info("graphiti.closed")

    # ── Readiness ──────────────────────────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        """``True`` when the Graphiti instance has been initialised."""
        return self._graphiti is not None

    async def health_check(self) -> bool:
        """Ping the FalkorDB backend to confirm connectivity.

        Uses a direct Redis PING against the FalkorDB URL.

        Returns:
            ``True`` if the backend is reachable, ``False`` otherwise.
        """
        if self._graphiti is None:
            return False

        try:
            sync_redis = RedisSync.from_url(self._falkordb_url, socket_timeout=5)
            loop = self._loop or asyncio.get_running_loop()
            result: bool = await loop.run_in_executor(None, sync_redis.ping)
            sync_redis.close()
            return result
        except Exception as exc:
            logger.warning("graphiti.ping_failed", extra={"error": str(exc)})
            return False

    # ── Accessor ───────────────────────────────────────────────────────────────

    @property
    def client(self) -> Graphiti:
        """Access the underlying ``Graphiti`` SDK instance.

        Raises:
            RuntimeError: If ``initialize()`` has not been called yet.
        """
        if self._graphiti is None:
            raise RuntimeError("Graphiti has not been initialised — call initialize() first")
        return self._graphiti


# ═══════════════════════════════════════════════════════════════════════════════
# Module-level singleton — managed by FastAPI lifespan hooks
# ═══════════════════════════════════════════════════════════════════════════════

_client: GraphitiClient | None = None


async def init_graphiti(
    falkordb_url: str | None = None,
    llm_client: Any | None = None,
    embedder: Any | None = None,
) -> GraphitiClient:
    """Initialise the global GraphitiClient singleton.

    Intended to be called from FastAPI's ``lifespan`` startup context.

    Args:
        falkordb_url: FalkorDB connection string (defaults to
            ``settings.FALKORDB_URL``).
        llm_client: Optional LLM client for Graphiti's entity extraction.
        embedder: Optional embedding model.

    Returns:
        The initialised ``GraphitiClient`` singleton.

    Raises:
        ExternalServiceError: If the backend is unreachable.
    """
    global _client
    if _client is not None:
        logger.warning("graphiti.reinitialization_attempted — closing existing client")
        await _client.close()

    _client = GraphitiClient(
        falkordb_url=falkordb_url,
        llm_client=llm_client,
        embedder=embedder,
    )
    await _client.initialize()
    return _client


async def close_graphiti() -> None:
    """Shut down the global GraphitiClient singleton.

    Intended to be called from FastAPI's ``lifespan`` shutdown context.
    Safe to call multiple times.
    """
    global _client
    if _client is not None:
        await _client.close()
        _client = None


def get_graphiti() -> GraphitiClient:
    """Retrieve the global GraphitiClient singleton.

    Returns:
        The initialised client.

    Raises:
        RuntimeError: If ``init_graphiti()`` has not been called.
    """
    if _client is None:
        raise RuntimeError(
            "Graphiti has not been initialised — call init_graphiti() during "
            "application startup before accessing the client."
        )
    return _client


@asynccontextmanager
async def graphiti_lifespan(
    falkordb_url: str | None = None,
    llm_client: Any | None = None,
    embedder: Any | None = None,
) -> AsyncGenerator[GraphitiClient, None]:
    """Async context manager wrapping the full lifecycle.

    Useful for one-off scripts, tests, or any context where you do not want
    to manage the module-level singleton::

        async with graphiti_lifespan() as g:
            await g.add_entity(...)
    """
    client = GraphitiClient(
        falkordb_url=falkordb_url,
        llm_client=llm_client,
        embedder=embedder,
    )
    try:
        await client.initialize()
        yield client
    finally:
        await client.close()
