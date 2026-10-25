"""Unit tests: webhook Transit failure paths (B2) + emit skip paths (W1, W3).

All I/O is mocked at the service boundary — no DB, Redis, or OpenBao.

Live-behavior contract (smoke-tested 2026-10-25):
- Transit failure on create/rotate → JSON 502 problem envelope
  (``ExternalServiceError``: status 502, code ``external_service_error``),
  never a plain-text 500 — and zero ``webhook_endpoints`` rows persisted.
- NULL-secret legacy row with ``transit=None`` → endpoint skipped,
  ``openzync_webhook_decrypt_skips_total{reason="backfill_no_transit"}``
  increments, row stays NULL (no plaintext write).
- Decrypt failure on one endpoint → skip counter increments, other
  endpoints still emit.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from core.exceptions import ExternalServiceError
from core.openbao_exceptions import OpenBaoError
from middleware.metrics import webhook_decrypt_skips_total
from services.webhook_service import WebhookService

pytestmark = pytest.mark.unit

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")


def _make_endpoint(**overrides: Any) -> MagicMock:
    """Build a WebhookEndpoint-shaped mock."""
    ep = MagicMock()
    ep.id = overrides.get("id", uuid4())
    ep.organization_id = overrides.get("organization_id", ORG_ID)
    ep.name = overrides.get("name", "hook")
    ep.url = overrides.get("url", "https://example.com/hook")
    ep.signing_secret = overrides.get("signing_secret")
    ep.is_active = True
    return ep


def _skip_count(reason: str) -> float:
    """Current value of the decrypt-skip counter for a reason."""
    return webhook_decrypt_skips_total.labels(reason=reason)._value.get()


def _make_service(
    transit: AsyncMock | None,
    repo: AsyncMock | None = None,
) -> tuple[WebhookService, AsyncMock]:
    mock_repo = repo or AsyncMock()
    return WebhookService(repo=mock_repo, transit=transit), mock_repo


class TestCreateTransitFailure:
    """B2: Transit failure on create → JSON 502, zero rows persisted."""

    @pytest.mark.asyncio
    async def test_create_transit_raises_502_envelope(self) -> None:
        """``encrypt_webhook_secret`` raising → ``ExternalServiceError`` 502."""
        transit = AsyncMock()
        transit.encrypt_webhook_secret.side_effect = ExternalServiceError(
            "Webhook secret encryption failed (OpenBao Transit 'webhook-secret').",
            detail={"key": "webhook-secret"},
        )
        service, repo = _make_service(transit)

        with pytest.raises(ExternalServiceError) as exc_info:
            await service.create_endpoint(
                organization_id=ORG_ID, name="h", url="https://x.example/h", events=[]
            )

        assert exc_info.value.status_code == 502
        assert exc_info.value.code == "external_service_error"
        # Zero rows persisted — encrypt happens BEFORE repo.create.
        repo.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_openbao_error_wrapped_as_502(self) -> None:
        """A raw ``OpenBaoError`` from Transit still surfaces as JSON 502."""
        transit = AsyncMock()
        transit.encrypt_webhook_secret.side_effect = OpenBaoError("transit down")
        service, repo = _make_service(transit)

        with pytest.raises(ExternalServiceError) as exc_info:
            await service.create_endpoint(
                organization_id=ORG_ID, name="h", url="https://x.example/h", events=[]
            )

        assert exc_info.value.status_code == 502
        repo.create.assert_not_awaited()


class TestRotateTransitFailure:
    """B2: Transit failure on rotate → JSON 502, row untouched."""

    @pytest.mark.asyncio
    async def test_rotate_transit_raises_502_row_untouched(self) -> None:
        """Rotate encrypt failure → 502 and no ``update`` write."""
        endpoint_id = uuid4()
        repo = AsyncMock()
        repo.get_by_id.return_value = _make_endpoint(
            id=endpoint_id, signing_secret="vault:v1:old"
        )
        transit = AsyncMock()
        transit.encrypt_webhook_secret.side_effect = ExternalServiceError("no transit")
        service, _ = _make_service(transit, repo)

        with pytest.raises(ExternalServiceError) as exc_info:
            await service.rotate_endpoint_secret(endpoint_id, ORG_ID)

        assert exc_info.value.status_code == 502
        repo.update.assert_not_awaited()


class TestEmitNullSecretNoTransit:
    """W1: NULL-secret legacy row with ``transit=None`` → skip + count."""

    @pytest.mark.asyncio
    async def test_null_secret_no_transit_skips_and_counts(self) -> None:
        """Endpoint skipped, counter increments, row stays NULL."""
        ep = _make_endpoint(signing_secret=None)
        repo = AsyncMock()
        repo.get_active_endpoints_for_event.return_value = [ep]
        service, _ = _make_service(None, repo)

        before = _skip_count("backfill_no_transit")
        arq_pool = AsyncMock()
        settings = MagicMock()
        settings.ENVIRONMENT = "test"
        with (
            patch("services.webhook_service.get_arq", return_value=arq_pool),
            patch("services.webhook_service.get_settings", return_value=settings),
        ):
            assert await service.emit(ORG_ID, "message.added", {}) is True

        arq_pool.enqueue.assert_not_awaited()
        assert _skip_count("backfill_no_transit") == before + 1
        # No plaintext write — the backfill path is never entered.
        repo.set_signing_secret_if_null.assert_not_awaited()
        repo.get_by_id.assert_not_awaited()
        assert ep.signing_secret is None


class TestEmitDecryptFailure:
    """W3: decrypt failure on one endpoint → skip counted, others emit."""

    @pytest.mark.asyncio
    async def test_decrypt_failure_skips_one_emits_other(self) -> None:
        """Bad endpoint skipped (counted); good endpoint still enqueued."""
        bad = _make_endpoint(signing_secret="vault:v1:bad")
        good = _make_endpoint(signing_secret="vault:v1:good")
        repo = AsyncMock()
        repo.get_active_endpoints_for_event.return_value = [bad, good]

        transit = AsyncMock()

        async def _decrypt(webhook_id: UUID, ciphertext: str) -> str:
            if "bad" in ciphertext:
                raise ExternalServiceError("decrypt failed")
            return "whsec_good"

        transit.decrypt_webhook_secret.side_effect = _decrypt
        service, _ = _make_service(transit, repo)

        arq_pool = AsyncMock()
        settings = MagicMock()
        settings.ENVIRONMENT = "test"
        before = _skip_count("decrypt_failed")
        with (
            patch("services.webhook_service.get_arq", return_value=arq_pool),
            patch("services.webhook_service.get_settings", return_value=settings),
        ):
            assert await service.emit(ORG_ID, "message.added", {}) is True

        assert _skip_count("decrypt_failed") == before + 1
        arq_pool.enqueue.assert_awaited_once()
        kwargs = arq_pool.enqueue.await_args.kwargs
        assert kwargs["endpoint_id"] == str(good.id)
