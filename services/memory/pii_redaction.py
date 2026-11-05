"""PII redaction helpers for memory ingestion — fail-closed, before the lock."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog

from core.exceptions import PIIUnavailableError, ValidationError

if TYPE_CHECKING:
    from services.pii_service import PIIService

logger = logging.getLogger(__name__)


class PIIRedactionMixin:
    """Fail-closed PII config fetch and message redaction."""

    _db: Any
    _bao_client: Any

    async def _get_org_pii_config(self, org_id: UUID) -> dict:
        """Fetch PII configuration for an org — OpenBao only, fail-closed.

        Any fetch failure raises ``PIIUnavailableError`` (→ 503 +
        ``Retry-After``) instead of falling back to a default: persisting
        content without a known redaction policy is worse than rejecting
        the request.  The legacy ``organizations.quotas -> 'pii'`` fallback
        was deleted (BREAKING for orgs that still store PII in quotas —
        migrate them to OpenBao org config).

        A successful fetch with ``pii_mode`` unset means "not configured",
        which keeps the previous effective default of ``mask``.

        Args:
            org_id: The organization UUID.

        Returns:
            The PII config dict.

        Raises:
            PIIUnavailableError: If the config cannot be fetched.
        """
        trace_id = structlog.contextvars.get_contextvars().get("request_id", "unknown")
        try:
            from core.org_config import get_org_config

            if self._bao_client is not None:
                org_cfg = await get_org_config(
                    org_id, redis=None, bao_client=self._bao_client
                )
            else:
                # Lazy temporary client — mirrors _process_blobs pattern.
                from core.config import BootstrapSettings
                from core.openbao import OpenBaoClient

                bootstrap = BootstrapSettings()
                async with OpenBaoClient(
                    bootstrap.OPENBAO_ADDR,
                    bootstrap.OPENBAO_ROLE_ID,
                    bootstrap.OPENBAO_SECRET_ID,
                    timeout=10.0,
                ) as _tmp_bao:
                    org_cfg = await get_org_config(
                        org_id, redis=None, bao_client=_tmp_bao
                    )
        except Exception as exc:
            logger.error(
                "pii.fail_closed",
                extra={"org_id": str(org_id), "trace_id": trace_id},
                exc_info=True,
            )
            raise PIIUnavailableError("pii_unavailable") from exc

        if org_cfg is None:
            logger.error(
                "pii.fail_closed",
                extra={"org_id": str(org_id), "trace_id": trace_id},
            )
            raise PIIUnavailableError("pii_unavailable")

        if org_cfg.pii_mode is None:
            return {"mode": "mask"}

        pii: dict[str, Any] = {"mode": org_cfg.pii_mode}
        if org_cfg.pii_sensitivity is not None:
            pii["sensitivity"] = org_cfg.pii_sensitivity
        if org_cfg.pii_enabled_types is not None:
            pii["enabled_types"] = org_cfg.pii_enabled_types
        if org_cfg.pii_min_confidence is not None:
            pii["min_confidence"] = org_cfg.pii_min_confidence
        return pii

    async def _redact_content(
        self,
        pii_service: PIIService,
        mask_service: PIIService,
        content: str,
        *,
        org_id: UUID,
        trace_id: str,
    ) -> str:
        """Redact PII from message content, fail-closed.

        Both services are built once per ingest by the caller and reused
        across messages.  A block-mode ``ValidationError`` downgrades to a
        single mask pass (preserved semantic — the message is still stored,
        redacted).  Any other redaction failure raises
        ``PIIUnavailableError``: the message is never persisted unredacted.
        Detections and content are never logged — only org/trace identifiers.

        Args:
            pii_service: The org-configured PII service (shared).
            mask_service: The mask-mode fallback for block downgrades (shared).
            content: The raw message content.
            org_id: The organization UUID (log context only).
            trace_id: The request trace ID (log context only).

        Returns:
            The redacted content (unchanged when nothing was detected).

        Raises:
            PIIUnavailableError: If redaction fails unexpectedly.
        """
        try:
            try:
                redacted, _, _ = await pii_service.process_message(content)
            except ValidationError:
                redacted, _, _ = await mask_service.process_message(content)
                logger.info(
                    "memory.pii_blocked_redacted",
                    extra={"org_id": str(org_id), "trace_id": trace_id},
                )
            return redacted
        except PIIUnavailableError:
            raise
        except Exception as exc:
            logger.error(
                "pii.fail_closed",
                extra={"org_id": str(org_id), "trace_id": trace_id},
            )
            raise PIIUnavailableError("pii_unavailable") from exc

    async def _redact_episodes(
        self,
        org_id: UUID,
        episode_dicts: list[dict[str, Any]],
        job_id: UUID,
    ) -> None:
        """Redact PII across episode dicts in place (bounded fan-out).

        Fetches the org PII config and redacts every message content
        before the row lock — network I/O never holds
        ``SELECT ... FOR UPDATE``. Fail-closed: any failure raises
        ``PIIUnavailableError`` and nothing is persisted.

        Args:
            org_id: The organization UUID.
            episode_dicts: Mutable per-message dicts (``content`` redacted
                in place).
            job_id: Ingest job ID (trace fallback).
        """
        from services.pii_service import PIIService

        pii_config_raw = await self._get_org_pii_config(org_id)
        pii_mode = (
            pii_config_raw.get("mode", "mask")
            if isinstance(pii_config_raw, dict)
            else "mask"
        )
        if pii_mode == "off":
            return
        # Built once per ingest, not per message — construction is
        # cheap but the NER pipeline it pulls in is not.
        pii_service = PIIService(pii_config_raw)
        mask_service = PIIService({**pii_config_raw, "mode": "mask"})
        trace_id = structlog.contextvars.get_contextvars().get(
            "request_id", str(job_id)
        )
        # Bounded fan-out: NER is CPU-heavy, so at most 8 messages
        # redact concurrently.  Fail-closed is preserved —
        # _redact_content raises and gather propagates.
        _redact_semaphore = asyncio.Semaphore(8)

        async def _redact_one(msg_dict: dict) -> None:
            async with _redact_semaphore:
                msg_dict["content"] = await self._redact_content(
                    pii_service,
                    mask_service,
                    msg_dict["content"],
                    org_id=org_id,
                    trace_id=trace_id,
                )

        await asyncio.gather(
            *(_redact_one(msg_dict) for msg_dict in episode_dicts)
        )
