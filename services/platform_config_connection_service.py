"""Connection testing for platform configuration candidates.

Overlays a candidate :class:`UpdatePlatformConfigRequest` in-memory on the
stored DB config and probes the requested domain against the live
backend.  Nothing is ever written to the DB or Redis by this service.

Probe transport notes:

- LLM goes through :func:`core.llm.resolve_backend` plus a minimal
  ``chat(max_tokens=1)`` round-trip.  Provider SDKs speak async HTTP
  (``httpx``) under the hood — there is no ``requests`` usage on this path.
- ⚠️ BREAKING: there is no embeddings probe.  Embeddings are a local ONNX
  model with nothing to configure or connect to.
- ⚠️ BREAKING: there is no graph probe.  Graph connectivity is env-owned
  (``OZ_FALKORDB_URL`` / ``OZ_SURREALDB_*``) with nothing per-user to
  configure or connect to — ``domain="graph"`` is rejected with 422.
- ⚠️ BREAKING: there is no blob probe. Blob storage is a single global
  backend (``OZ_S3_*`` Settings) with nothing per-user to configure or
  connect to — ``domain="blob"`` is rejected with 422.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import ValidationError
from core.platform_config import get_platform_config
from schemas.platform_config import (
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
        as_user: UUID | None = None,
    ) -> TestPlatformConfigResponse:
        """Probe live connectivity for one config domain.

        Reads the stored config (cache bypassed), overlays the fields
        set in *candidate*, and runs the domain probe.  Probe failures
        yield ``ok: False`` entries — never exceptions.

        Args:
            user_id: The user UUID.
            domain: One of ``llm``.
            candidate: Partial candidate config; only ``exclude_unset``
                fields overlay the stored config.
            as_user: Optional user whose stored override is layered under
                the candidate (``candidate ⊕ user ⊕ stored``), in-memory
                only — nothing is persisted.

        Returns:
            A :class:`TestPlatformConfigResponse` with one entry per probe.

        Raises:
            ValidationError: If *domain* is unknown.
        """
        if as_user is not None:
            from repositories.user_repository import UserRepository
            from services.user_config_service import UserConfigService

            override = await UserConfigService(
                user_repo=UserRepository(self._db),
                redis=self._redis,
                db=self._db,
            ).get_override(as_user)
            if override:
                candidate = UpdatePlatformConfigRequest(
                    **{**override, **candidate.model_dump(exclude_unset=True)}
                )
        stored = await get_platform_config(
            user_id,
            redis=self._redis,
            db=self._db,
            skip_cache=True,
        )
        merged = self._overlay_candidate(stored, candidate)
        logger.info(
            "platform_config.test_started",
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
            "platform_config.test_finished",
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
        raise ValidationError(
            f"Unknown config test domain: {domain!r}. Expected one of: llm."
            # ⚠️ BREAKING: the graph probe was removed with the per-user
            # graph connection fields — graph connectivity is env-owned
            # (OZ_FALKORDB_URL / OZ_SURREALDB_*) now.
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

            backend = await resolve_backend(config=merged.to_llm_config())
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

    @staticmethod
    def _success(start: float, detail: str) -> ProbeResult:
        """Build a successful :class:`ProbeResult`."""
        return ProbeResult(ok=True, latency_ms=_elapsed_ms(start), detail=detail)

    @classmethod
    def _failure(cls, start: float, detail: str) -> ProbeResult:
        """Build a failed :class:`ProbeResult` (detail redacted/truncated)."""
        redacted = cls._redact(detail)
        logger.warning("platform_config.probe_failed", extra={"detail": redacted})
        return ProbeResult(ok=False, latency_ms=_elapsed_ms(start), detail=redacted)

    @staticmethod
    def _redact(text: str) -> str:
        """Strip embedded credentials from URLs, then truncate."""
        redacted = _SECRET_IN_URL_RE.sub(r"\1***@", text)
        return redacted[:_DETAIL_MAX_LEN]
