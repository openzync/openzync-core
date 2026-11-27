"""Connection testing for per-organization config candidates.

Overlays a candidate :class:`UpdatePlatformConfigRequest` in-memory on the
stored DB config and probes the requested domain against the live
backend.  Nothing is ever written to the DB or Redis by this service.

Probe transport notes:

- LLM goes through :func:`core.llm.resolve_backend` plus a minimal
  ``chat(max_tokens=1)`` round-trip.  Provider SDKs speak async HTTP
  (``httpx``) under the hood — there is no ``requests`` usage on this path.
- ⚠️ BREAKING: there is no embeddings probe.  Embeddings are a local ONNX
  model with nothing to configure or connect to.
- Graph goes through :meth:`GraphBackendDispatcher.resolve_and_create`
  followed by ``health_check()``, with a candidate-scoped client built
  from the effective (candidate → system) URL.
- ⚠️ BREAKING: there is no blob probe. Blob storage is a single global
  backend (``OZ_S3_*`` Settings) with nothing per-org to configure or
  connect to — ``domain="blob"`` is rejected with 422.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.exceptions import ValidationError
from core.graph_backend import GraphBackendDispatcher, init_dispatcher
from core.org_config import get_platform_config
from schemas.organization_config import (
    SYSTEM_MANAGED_FALKORDB_FIELDS,
    SYSTEM_MANAGED_SURREALDB_FIELDS,
    PlatformConfigBase,
    ProbeResult,
    TestPlatformConfigResponse,
    UpdatePlatformConfigRequest,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from uuid import UUID

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT_S = 8.0
"""Upper bound in seconds for any single domain probe."""

_DETAIL_MAX_LEN = 300
"""Max characters kept from a provider error before truncation."""

_SECRET_IN_URL_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^@\s/]+@")


def _elapsed_ms(start: float) -> int:
    """Return whole milliseconds elapsed since *start* (``perf_counter``)."""
    return max(0, int((time.perf_counter() - start) * 1000))


class PlatformConfigConnectionService:
    """Probe live backend connectivity for a candidate platform config.

    Args:
        db: Async DB session for the stored-config read (cache bypassed).
        redis: Accepted for constructor symmetry with
            :class:`PlatformConfigService`; unused — probes bypass the cache.
    """

    def __init__(self, db: AsyncSession, redis: Any | None = None) -> None:
        self._db = db
        self._redis = redis

    async def test_connections(
        self,
        user_id: UUID,
        domain: str,
        candidate: UpdatePlatformConfigRequest,
    ) -> TestPlatformConfigResponse:
        """Probe live connectivity for one config domain.

        Reads the stored config (cache bypassed), overlays the fields
        set in *candidate*, and runs the domain probe.  Probe failures
        yield ``ok: False`` entries — never exceptions.

        Args:
            user_id: The organization UUID.
            domain: One of ``llm``, ``graph``.
            candidate: Partial candidate config; only ``exclude_unset``
                fields overlay the stored config.

        Returns:
            A :class:`TestPlatformConfigResponse` with one entry per probe.

        Raises:
            ValidationError: If *candidate* overrides system-managed
                fields, or *domain* is unknown.
        """
        self._reject_system_managed_overrides(candidate)
        stored = await get_platform_config(
            user_id,
            redis=self._redis,
            db=self._db,
            skip_cache=True,
        )
        merged = self._overlay_candidate(stored, candidate)
        logger.info(
            "org_config.test_started",
            extra={"user_id": str(user_id), "domain": domain},
        )
        probe = self._resolve_probe(domain)
        results = await asyncio.gather(
            asyncio.wait_for(probe(merged), timeout=_PROBE_TIMEOUT_S),
            return_exceptions=True,
        )
        outcome = results[0]
        if isinstance(outcome, asyncio.CancelledError):
            raise outcome
        if isinstance(outcome, ProbeResult):
            result = outcome
        elif isinstance(outcome, TimeoutError):
            result = ProbeResult(
                ok=False,
                latency_ms=int(_PROBE_TIMEOUT_S * 1000),
                detail=f"{domain} probe timed out after {_PROBE_TIMEOUT_S:.0f}s",
            )
        elif isinstance(outcome, Exception):
            result = ProbeResult(
                ok=False, latency_ms=0, detail=self._redact(str(outcome))
            )
        else:
            raise outcome
        logger.info(
            "org_config.test_finished",
            extra={
                "user_id": str(user_id),
                "domain": domain,
                "ok": result.ok,
                "latency_ms": result.latency_ms,
            },
        )
        return TestPlatformConfigResponse(results={domain: result})

    def _resolve_probe(
        self, domain: str
    ) -> Callable[[PlatformConfigBase], Awaitable[ProbeResult]]:
        """Return the probe coroutine for *domain*.

        Raises:
            ValidationError: If *domain* is not a known test domain.
        """
        match domain:
            case "llm":
                return self._probe_llm
            case "graph":
                return self._probe_graph
        raise ValidationError(
            f"Unknown config test domain: {domain!r}. Expected one of: llm, graph."
            # ⚠️ BREAKING: the blob probe was removed with per-org blob
            # storage — blob storage is global (OZ_S3_*) now.
        )

    @staticmethod
    def _reject_system_managed_overrides(
        candidate: UpdatePlatformConfigRequest,
    ) -> None:
        """Reject candidates that override system-managed URL fields.

        Mirrors the enforcement in ``routers/admin_org_config.py`` without
        importing from the router layer (wrong dependency direction).

        Raises:
            ValidationError: If any set candidate field is system-managed.
        """
        settings = get_settings()
        managed: set[str] = set()
        if settings.SURREALDB_URL:
            managed |= set(SYSTEM_MANAGED_SURREALDB_FIELDS)
        if settings.FALKORDB_URL:
            managed |= set(SYSTEM_MANAGED_FALKORDB_FIELDS)
        if not managed:
            return
        overridden = managed.intersection(candidate.model_dump(exclude_unset=True))
        if overridden:
            raise ValidationError(
                "These fields are configured at the system level "
                "and cannot be modified: "
                f"{', '.join(sorted(overridden))}."
            )

    @staticmethod
    def _overlay_candidate(
        stored: PlatformConfigBase, candidate: UpdatePlatformConfigRequest
    ) -> PlatformConfigBase:
        """Overlay set candidate fields on the stored config (in-memory).

        Explicit ``None`` values override stored values (same semantics
        as PATCH).  Nested objects (``prompt_caching``) replace wholesale.
        """
        merged_data = {
            **stored.model_dump(),
            **candidate.model_dump(exclude_unset=True),
        }
        return PlatformConfigBase(**merged_data)

    async def _probe_llm(self, merged: PlatformConfigBase) -> ProbeResult:
        """Minimal chat round-trip (``max_tokens=1``) via resolved backend."""
        start = time.perf_counter()
        try:
            from core.llm import resolve_backend

            backend = await resolve_backend(org_config=merged.to_llm_config_dict())
            chat_kwargs: dict[str, Any] = {"max_tokens": 1}
            if merged.llm_model is not None:
                chat_kwargs["model"] = merged.llm_model
            await backend.chat(
                [{"role": "user", "content": "ping"}],
                metered=False,
                **chat_kwargs,
            )
            return self._success(start, f"llm ok via {backend.model_name}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._failure(start, f"llm probe failed: {exc}")

    async def _probe_graph(self, merged: PlatformConfigBase) -> ProbeResult:
        """Resolve the candidate graph backend and run ``health_check()``."""
        start = time.perf_counter()
        try:
            dispatcher = init_dispatcher()
            backend_name = dispatcher.resolve_backend_name(merged)
            if backend_name is None:
                return self._success(start, "graph disabled (backend 'none')")
            match backend_name:
                case "falkordb":
                    return await self._probe_falkordb(merged, dispatcher, start)
                case "surrealdb":
                    return await self._probe_surrealdb(merged, dispatcher, start)
                case _:
                    return self._failure(
                        start,
                        "graph probe failed: unsupported backend "
                        f"{backend_name!r} (postgres was removed in v1.1.0, "
                        "migrate to falkordb)",
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._failure(start, f"graph probe failed: {exc}")

    async def _probe_falkordb(
        self,
        merged: PlatformConfigBase,
        dispatcher: GraphBackendDispatcher,
        start: float,
    ) -> ProbeResult:
        """Health-check FalkorDB through a candidate-scoped client."""
        from falkordb.asyncio import FalkorDB
        from redis.asyncio import BlockingConnectionPool

        url = merged.falkordb_url or get_settings().FALKORDB_URL
        if not url:
            return self._failure(
                start, "graph probe failed: falkordb_url not configured"
            )
        pool = BlockingConnectionPool.from_url(url, max_connections=2, socket_timeout=5)
        client = FalkorDB(connection_pool=pool)
        try:
            # note: db is unused by the falkordb/surrealdb backends —
            # None keeps this service SQLAlchemy-free by construction.
            backend = dispatcher.resolve_and_create(
                merged,
                None,
                falkordb_client=client,  # type: ignore[arg-type]
            )
            if backend is None:
                return self._failure(start, "graph probe failed: backend disabled")
            healthy = await backend.health_check()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._failure(start, f"graph probe failed: {exc}")
        finally:
            try:
                await pool.aclose()
            except Exception:
                logger.warning("org_config.test_pool_close_failed", exc_info=True)
        if not healthy:
            return self._failure(
                start, "graph probe failed: FalkorDB health check false"
            )
        return self._success(start, "graph ok (falkordb)")

    async def _probe_surrealdb(
        self,
        merged: PlatformConfigBase,
        dispatcher: GraphBackendDispatcher,
        start: float,
    ) -> ProbeResult:
        """Health-check SurrealDB through a candidate-scoped connection."""
        from surrealdb import AsyncSurreal

        from core.surreal_pool import (
            DEFAULT_SURREALDB_DATABASE,
            DEFAULT_SURREALDB_NAMESPACE,
            DEFAULT_SURREALDB_PASS,
            DEFAULT_SURREALDB_USER,
        )

        url = merged.surrealdb_url or get_settings().SURREALDB_URL
        if not url:
            return self._failure(
                start, "graph probe failed: surrealdb_url not configured"
            )
        surreal = AsyncSurreal(url)
        try:
            await surreal.connect(url)
            await surreal.signin(
                {
                    "username": merged.surrealdb_user or DEFAULT_SURREALDB_USER,
                    "password": merged.surrealdb_pass or DEFAULT_SURREALDB_PASS,
                }
            )
            await surreal.use(
                merged.surrealdb_namespace or DEFAULT_SURREALDB_NAMESPACE,
                merged.surrealdb_database or DEFAULT_SURREALDB_DATABASE,
            )
            # note: db is unused by the falkordb/surrealdb backends —
            # None keeps this service SQLAlchemy-free by construction.
            backend = dispatcher.resolve_and_create(
                merged,
                None,
                surreal=surreal,  # type: ignore[arg-type]
            )
            if backend is None:
                return self._failure(start, "graph probe failed: backend disabled")
            healthy = await backend.health_check()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._failure(start, f"graph probe failed: {exc}")
        finally:
            try:
                await surreal.close()
            except Exception:
                logger.warning("org_config.test_surreal_close_failed", exc_info=True)
        if not healthy:
            return self._failure(
                start, "graph probe failed: SurrealDB health check false"
            )
        return self._success(start, "graph ok (surrealdb)")

    @staticmethod
    def _success(start: float, detail: str) -> ProbeResult:
        """Build a successful :class:`ProbeResult`."""
        return ProbeResult(ok=True, latency_ms=_elapsed_ms(start), detail=detail)

    @classmethod
    def _failure(cls, start: float, detail: str) -> ProbeResult:
        """Build a failed :class:`ProbeResult` (detail redacted/truncated)."""
        redacted = cls._redact(detail)
        logger.warning("org_config.probe_failed", extra={"detail": redacted})
        return ProbeResult(ok=False, latency_ms=_elapsed_ms(start), detail=redacted)

    @staticmethod
    def _redact(text: str) -> str:
        """Strip embedded credentials from URLs, then truncate."""
        redacted = _SECRET_IN_URL_RE.sub(r"\1***@", text)
        return redacted[:_DETAIL_MAX_LEN]
