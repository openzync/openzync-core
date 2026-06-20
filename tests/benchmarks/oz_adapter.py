"""OpenZep benchmark adapter — wraps the SDK + direct DB for enrichment polling.

The memory ingest endpoint (POST /v1/projects/{project_id}/memory) requires
JWT authentication (dashboard user session) because it uses
``get_current_user_id`` for message attribution.  API keys alone cannot call
this endpoint.

This adapter therefore authenticates with the benchmark user's email/password
to obtain a JWT token, and passes that token to the SDK as the Bearer token.
The SDK sends ``Authorization: Bearer <jwt>``, which the server's auth
middleware correctly identifies as JWT (it has two dots and does not start
with ``mg_live_``).

Usage::

    async with OpenZepBenchAdapter() as adapter:
        # Ingest messages into a session
        resp = await adapter.ingest(session_id, messages)

        # Wait for enrichment
        result = await adapter.wait_for_enrichment(session_id)

        # Get context for a query
        ctx = await adapter.get_context(query)

        # Clean run
        await adapter.clean_project()
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import httpx
from httpx import HTTPStatusError
from openzep import AsyncOpenZep
from openzep.models.memory import ContextResponse, IngestMemoryResponse, Message
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.benchmarks.enrichment_waiter import wait_for_session_enrichment

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

BENCH_DIR = Path(__file__).resolve().parent
ENV_FILE = BENCH_DIR / ".env"

SIGNUP_EMAIL: str = "benchmark@openzep.ai"
SIGNUP_PASSWORD: str = "bench-p@ssword-2026"
"""Benchmark dashboard user credentials — used for JWT auth."""


# ── Environment helpers ────────────────────────────────────────────────────────


def _load_env() -> dict[str, str]:
    """Load benchmark config from env (with .env fallback)."""
    config = {
        "project_id": os.getenv("BENCH_PROJECT_ID", ""),
        "base_url": os.getenv("OPENZEP_BASE_URL", "http://localhost:8000"),
    }

    # Fallback to .env file if vars are empty
    if not config["project_id"]:
        env_path = os.getenv("BENCH_ENV_FILE", str(ENV_FILE))
        try:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        key, _, value = line.partition("=")
                        key = key.strip()
                        value = value.strip().strip("'\"")
                        if key == "BENCH_PROJECT_ID" and not config["project_id"]:
                            config["project_id"] = value
                        if key == "OPENZEP_BASE_URL" and not config["base_url"]:
                            config["base_url"] = value
        except FileNotFoundError:
            pass

    if not config["project_id"]:
        raise RuntimeError(
            "Missing BENCH_PROJECT_ID. Run `python -m tests.benchmarks.setup` first "
            "or set BENCH_PROJECT_ID environment variable."
        )

    return config


# ── JWT auth ───────────────────────────────────────────────────────────────────


async def _login(base_url: str) -> str:
    """Login to OpenZep and return a JWT access token.

    Retries with exponential backoff on 429 (rate-limited) and 5xx errors.
    """
    import asyncio

    max_retries = 10
    base_delay = 5.0
    last_exc: Exception | None = None

    for attempt in range(max_retries):
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base_url}/v1/auth/login",
                json={"email": SIGNUP_EMAIL, "password": SIGNUP_PASSWORD},
            )
            if resp.status_code == 200:
                return resp.json()["access_token"]
            if resp.status_code == 429 or resp.status_code >= 500:
                last_exc = HTTPStatusError(
                    f"Login failed: {resp.status_code}",
                    request=resp.request,
                    response=resp,
                )
                delay = base_delay * (2**attempt)
                logger.info(
                    "bench.login_retry",
                    extra={
                        "status": resp.status_code,
                        "attempt": attempt + 1,
                        "delay_s": min(delay, 120),
                    },
                )
                await asyncio.sleep(min(delay, 120))
                continue
            resp.raise_for_status()

    raise last_exc or RuntimeError("Login failed after max retries")


# ── Adapter ─────────────────────────────────────────────────────────────────────


class OpenZepBenchAdapter:
    """Benchmark adapter wrapping the OpenZep SDK + enrichment waiter.

    Manages the connection lifecycle and provides high-level operations
    for benchmark ingestion, enrichment polling, and context retrieval.

    Uses JWT (dashboard session) auth — authenticates with the benchmark
    user's email/password, obtains a JWT, and passes it to the SDK as
    the Bearer token.  If a 401 is encountered the token is refreshed once.
    """

    def __init__(
        self,
        db_session_factory: async_sessionmaker | None = None,
        *,
        project_id: str | None = None,
        base_url: str | None = None,
    ) -> None:
        cfg = _load_env()
        self._project_id = project_id or cfg["project_id"]
        self._base_url = base_url or cfg["base_url"]
        self._db_session_factory = db_session_factory
        self._client: AsyncOpenZep | None = None
        self._jwt_token: str | None = None

    # ── Properties ──────────────────────────────────────────────────────────

    @property
    def project_id(self) -> str:
        """The benchmark project UUID."""
        return self._project_id

    @property
    def client(self) -> AsyncOpenZep:
        """The underlying SDK client (raises if not connected)."""
        if self._client is None:
            raise RuntimeError("Adapter not connected — use 'async with'")
        return self._client

    # ── Lifecycle ───────────────────────────────────────────────────────────

    async def __aenter__(self) -> OpenZepBenchAdapter:
        self._jwt_token = await _login(self._base_url)
        logger.info("bench.jwt_login", extra={"base_url": self._base_url})
        # Pass the JWT token as the "api_key" — the SDK sends Bearer <token>
        # regardless, and the server auth middleware detects it as JWT.
        self._client = AsyncOpenZep(
            api_key=self._jwt_token,
            base_url=self._base_url,
            timeout=60.0,
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def close(self) -> None:
        """Explicit close (for non-context-manager usage)."""
        if self._client is not None:
            await self._client.close()
            self._client = None

    # ── Token refresh ───────────────────────────────────────────────────────

    async def _refresh_token(self) -> None:
        """Re-login and replace the JWT token in the SDK client.

        Called when a 401 is encountered (access token expired — default
        JWT TTL is 15 min for access tokens).
        """
        self._jwt_token = await _login(self._base_url)
        logger.info("bench.jwt_refresh", extra={"base_url": self._base_url})
        if self._client is not None:
            await self._client.close()
        self._client = AsyncOpenZep(
            api_key=self._jwt_token,
            base_url=self._base_url,
            timeout=60.0,
        )

    # ── Core operations ─────────────────────────────────────────────────────

    async def _ensure_session(self, session_id: str) -> None:
        """Create session if it doesn't exist.  Idempotent on duplicate."""
        try:
            await self.client.sessions.create(
                project_id=self._project_id,
                external_id=session_id,
            )
        except Exception as exc:
            exc_str = str(exc)
            # 409 Conflict = session already exists — fine
            if "409" not in exc_str and "Conflict" not in exc_str and "already exists" not in exc_str:
                raise

    async def ingest(
        self,
        session_id: str,
        messages: list[dict[str, str]],
        *,
        idempotency_key: str | None = None,
    ) -> IngestMemoryResponse:
        """Ingest messages into a session.

        Creates the session first if it doesn't already exist.

        Args:
            session_id: External ID for the session (created if needed).
            messages: List of ``{"role": ..., "content": ...}`` dicts.
            idempotency_key: Optional dedup key.

        Returns:
            ``IngestMemoryResponse`` with job_id and episode count.

        Note:
            On 401 (expired JWT), refreshes the token once and retries.
        """
        await self._ensure_session(session_id)
        msg_objects = [
            Message(role=m["role"], content=m["content"])
            for m in messages
        ]
        try:
            return await self.client.memory.ingest(
                project_id=self._project_id,
                messages=msg_objects,
                session_id=session_id,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            # Check for 401 — expired JWT token
            exc_str = str(exc)
            if "401" in exc_str or "Unauthorized" in exc_str or "Authentication" in exc_str:
                logger.info("bench.token_expired_refreshing")
                await self._refresh_token()
                return await self.client.memory.ingest(
                    project_id=self._project_id,
                    messages=msg_objects,
                    session_id=session_id,
                    idempotency_key=idempotency_key,
                )
            raise

    async def wait_for_enrichment(
        self,
        session_id: str,
        *,
        timeout_s: float = 300.0,
    ) -> dict[str, Any]:
        """Wait for all episodes in a session to be fully enriched.

        Requires ``db_session_factory`` to have been provided at construction.

        Returns:
            Dict with ``elapsed_s``, ``episode_count``, ``fully_enriched``.
        """
        if self._db_session_factory is None:
            raise RuntimeError(
                "db_session_factory required for enrichment polling — "
                "pass it to OpenZepBenchAdapter.__init__"
            )
        return await wait_for_session_enrichment(
            session_id=session_id,
            db_session_factory=self._db_session_factory,
            timeout_s=timeout_s,
        )

    async def get_context(
        self,
        query: str,
        *,
        limit: int = 20,
    ) -> ContextResponse:
        """Assemble a context block for an LLM query.

        Args:
            query: Natural-language query describing the context needed.
            limit: Maximum items per source type.

        Returns:
            ``ContextResponse`` with formatted context string.
        """
        return await self.client.memory.get_context(
            project_id=self._project_id,
            query=query,
            limit=limit,
        )

    async def search(
        self,
        query: str,
        *,
        types: str = "episodes,facts",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Hybrid search across project memory.

        Args:
            query: Search query string.
            types: Comma-separated result types.
            limit: Maximum results per type.

        Returns:
            List of search result dicts.
        """
        return await self.client.graph.search(
            project_id=self._project_id,
            query=query,
            types=types,
            limit=limit,
        )

    async def clean_project(self) -> None:
        """Delete all memory for the benchmark project.

        Call this between benchmark runs to reset state.
        """
        await self.client.memory.delete(project_id=self._project_id)
        logger.info("bench.clean_project", extra={"project_id": self._project_id})
