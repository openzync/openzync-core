"""Connection testing for per-organization config candidates.

Overlays a candidate :class:`UpdateOrgConfigRequest` in-memory on the
stored OpenBao config and probes the requested domain against the live
backend.  Nothing is ever written to OpenBao or Redis by this service.

Probe transport notes:

- LLM / embeddings go through :func:`core.llm.resolve_backend` plus a
  minimal ``chat(max_tokens=1)`` / ``embed(["ping"])`` round-trip.  All
  provider SDKs speak async HTTP (``httpx``) under the hood — there is
  no ``requests`` usage anywhere on this path.
- Graph goes through :meth:`GraphBackendDispatcher.resolve_and_create`
  followed by ``health_check()``, with a candidate-scoped client built
  from the effective (candidate → system) URL.
- Blob runs S3 ``head_bucket`` against the candidate endpoint.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from core.config import get_settings
from core.exceptions import ValidationError
from core.graph_backend import GraphBackendDispatcher, init_dispatcher
from core.openbao_exceptions import OpenBaoConnectionError
from core.org_config import get_org_config
from schemas.organization_config import (
    SYSTEM_MANAGED_FALKORDB_FIELDS,
    SYSTEM_MANAGED_SURREALDB_FIELDS,
    OrgConfigBase,
    ProbeResult,
    TestOrgConfigResponse,
    UpdateOrgConfigRequest,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from uuid import UUID

    from core.blob_storage import BlobStorageConfig
    from core.openbao import OpenBaoClient

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT_S = 8.0
"""Upper bound in seconds for any single domain probe."""

_DETAIL_MAX_LEN = 300
"""Max characters kept from a provider error before truncation."""

_SECRET_IN_URL_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^@\s/]+@")


def _elapsed_ms(start: float) -> int:
    """Return whole milliseconds elapsed since *start* (``perf_counter``)."""
    return max(0, int((time.perf_counter() - start) * 1000))


class OrgConfigConnectionService:
    """Probe live backend connectivity for a candidate org config.

    Args:
        bao_client: An authenticated :class:`OpenBaoClient`.  Required —
            the stored config is always read fresh (cache bypassed).
        redis: Accepted for constructor symmetry with
            :class:`OrgConfigService`; unused — probes bypass the cache.
    """

    def __init__(self, bao_client: OpenBaoClient, redis: Any | None = None) -> None:
        self._bao_client = bao_client
        self._redis = redis

    async def test_connections(
        self,
        org_id: UUID,
        domain: str,
        candidate: UpdateOrgConfigRequest,
    ) -> TestOrgConfigResponse:
        """Probe live connectivity for one config domain.

        Reads the stored config (cache bypassed), overlays the fields
        set in *candidate*, and runs the domain probe.  Probe failures
        yield ``ok: False`` entries — never exceptions.

        Args:
            org_id: The organization UUID.
            domain: One of ``llm``, ``embeddings``, ``graph``, ``blob``.
            candidate: Partial candidate config; only ``exclude_unset``
                fields overlay the stored config.

        Returns:
            A :class:`TestOrgConfigResponse` with one entry per probe.

        Raises:
            OpenBaoConnectionError: If the secrets backend is unreachable.
            ValidationError: If *candidate* overrides system-managed
                fields, or *domain* is unknown.
        """
        if self._bao_client is None:
            raise OpenBaoConnectionError("OpenBao client required for org config test")
        self._reject_system_managed_overrides(candidate)
        stored = await get_org_config(
            org_id,
            redis=self._redis,
            bao_client=self._bao_client,
            skip_cache=True,
        )
        merged = self._overlay_candidate(stored, candidate)
        logger.info(
            "org_config.test_started",
            extra={"org_id": str(org_id), "domain": domain},
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
                "org_id": str(org_id),
                "domain": domain,
                "ok": result.ok,
                "latency_ms": result.latency_ms,
            },
        )
        return TestOrgConfigResponse(results={domain: result})

    def _resolve_probe(
        self, domain: str
    ) -> Callable[[OrgConfigBase], Awaitable[ProbeResult]]:
        """Return the probe coroutine for *domain*.

        Raises:
            ValidationError: If *domain* is not a known test domain.
        """
        match domain:
            case "llm":
                return self._probe_llm
            case "embeddings":
                return self._probe_embeddings
            case "graph":
                return self._probe_graph
            case "blob":
                return self._probe_blob
        raise ValidationError(
            f"Unknown config test domain: {domain!r}. "
            "Expected one of: llm, embeddings, graph, blob."
        )

    @staticmethod
    def _reject_system_managed_overrides(
        candidate: UpdateOrgConfigRequest,
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
        stored: OrgConfigBase, candidate: UpdateOrgConfigRequest
    ) -> OrgConfigBase:
        """Overlay set candidate fields on the stored config (in-memory).

        Explicit ``None`` values override stored values (same semantics
        as PATCH).  Nested objects (``prompt_caching``) replace wholesale.
        """
        merged_data = {
            **stored.model_dump(),
            **candidate.model_dump(exclude_unset=True),
        }
        return OrgConfigBase(**merged_data)

    async def _probe_llm(self, merged: OrgConfigBase) -> ProbeResult:
        """Minimal chat round-trip (``max_tokens=1``) via resolved backend."""
        start = time.perf_counter()
        try:
            from core.llm import resolve_backend

            backend = await resolve_backend(org_config=merged.to_llm_config_dict())
            await backend.chat([{"role": "user", "content": "ping"}], max_tokens=1)
            return self._success(start, f"llm ok via {backend.model_name}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._failure(start, f"llm probe failed: {exc}")

    async def _probe_embeddings(self, merged: OrgConfigBase) -> ProbeResult:
        """Single-vector embed plus frozen-dimension check.

        Embeds with the frozen canonical model
        (``core.embeddings.resolve_embed_model``) and requires exactly
        ``CANONICAL_EMBED_DIM`` dims. Dim-incompatible providers fail the
        probe — per-org ``embedding_model``/``embedding_dim`` overrides are
        frozen and ignored here.
        """
        start = time.perf_counter()
        if not merged.embedding_backend:
            return self._failure(
                start, "embeddings probe failed: embedding_backend is not configured"
            )
        try:
            from core.embeddings import CANONICAL_EMBED_DIM, resolve_embed_model
            from core.llm import resolve_backend

            backend = await resolve_backend(
                provider=merged.embedding_backend,
                org_config=merged.to_llm_config_dict(),
                mode="embedding",
            )
            model = resolve_embed_model(merged.embedding_backend)
            response = await backend.embed(["ping"], model=model)
            vectors = response.embeddings
            if not vectors or not vectors[0]:
                return self._failure(start, "embeddings probe failed: empty response")
            dim = len(vectors[0])
            if dim != CANONICAL_EMBED_DIM:
                return self._failure(
                    start,
                    "embeddings dim mismatch: got "
                    f"{dim}, expected canonical {CANONICAL_EMBED_DIM} "
                    f"(model={model})",
                )
            return self._success(
                start, f"embeddings ok dim={dim} model={response.model}"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._failure(start, f"embeddings probe failed: {exc}")

    async def _probe_graph(self, merged: OrgConfigBase) -> ProbeResult:
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
        merged: OrgConfigBase,
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
        merged: OrgConfigBase,
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

    async def _probe_blob(self, merged: OrgConfigBase) -> ProbeResult:
        """Run S3 ``head_bucket`` against the candidate endpoint."""
        start = time.perf_counter()
        try:
            from core.blob_storage import BlobStorageConfig

            storage_config = BlobStorageConfig.from_org_config(
                merged.to_blob_storage_config()
            )
            if (storage_config.backend or "s3") == "none":
                return self._success(start, "blob storage disabled (backend 'none')")
            if not storage_config.bucket_name:
                return self._failure(
                    start, "blob probe failed: s3_bucket_name not configured"
                )
            await self._head_bucket(storage_config)
            return self._success(
                start, f"blob ok (bucket '{storage_config.bucket_name}')"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._failure(start, f"blob probe failed: {exc}")

    @staticmethod
    async def _head_bucket(config: BlobStorageConfig) -> None:
        """Run S3 ``HeadBucket``; raises on any failure.

        Args:
            config: Candidate blob storage config (never logged).
        """
        import aioboto3  # lazy: optional dependency, only imported when used
        from botocore.config import Config as BotoConfig

        session = aioboto3.Session(
            aws_access_key_id=config.access_key_id,
            aws_secret_access_key=config.secret_access_key,
            region_name=config.region,
        )
        boto_config = BotoConfig(
            connect_timeout=5, read_timeout=5, retries={"max_attempts": 1}
        )
        async with session.client(
            "s3", endpoint_url=config.endpoint_url, config=boto_config
        ) as s3:
            await s3.head_bucket(Bucket=config.bucket_name)

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
