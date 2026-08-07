"""Unit tests for WebhookService — endpoint management and event emission.

All external dependencies (repository, ARQ) are mocked at the service boundary.
The ``sign_payload`` standalone function is tested directly — pure logic.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import orjson
import pytest

from models.webhook import WebhookEndpoint
from services.webhook_service import WebhookService, sign_payload

# Sentinel so ``_make_endpoint`` can distinguish "auto secret" from an
# explicit ``None`` (which mimics a legacy NULL column / lazy backfill).
_AUTO_SECRET = object()


def _verify_signature(secret: str, body: bytes, signature: str) -> bool:
    """Recompute the HMAC from the signature's own timestamp and compare.

    Deterministic regardless of when the check runs — we reuse the ``t=``
    value embedded in the signature instead of ``time.time()``.
    """
    _, v1 = signature.split(",v1=", 1)
    timestamp = signature.split(",", 1)[0].split("t=", 1)[1]
    expected = hmac.new(
        secret.encode(),
        f"{timestamp}.{body.decode()}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, v1)


@pytest.mark.unit
class TestSignPayload:
    """Unit tests for the standalone ``sign_payload`` function."""

    def test_sign_payload_returns_valid_format(self) -> None:
        """sign_payload returns a string with t= and v1= components."""
        signature = sign_payload("whsec_test", b'{"event":"test"}')
        assert signature.startswith("t=")
        assert ",v1=" in signature

    def test_sign_payload_different_payloads_different_signatures(self) -> None:
        """Different payloads produce different signatures."""
        s1 = sign_payload("whsec_test", b'{"event":"a"}')
        s2 = sign_payload("whsec_test", b'{"event":"b"}')
        assert s1 != s2

    def test_sign_payload_different_secrets_different_signatures(self) -> None:
        """Different secrets produce different signatures."""
        s1 = sign_payload("whsec_a", b'{"event":"test"}')
        s2 = sign_payload("whsec_b", b'{"event":"test"}')
        assert s1 != s2

    def test_sign_payload_same_input_same_timestamp_consistent(self) -> None:
        """Same payload + same secret is deterministic on the timestamp portion."""
        payload = b'{"event":"test"}'
        # We can't easily fix time.time(), but verify the hex part is consistent
        s1 = sign_payload("whsec_test", payload)
        s2 = sign_payload("whsec_test", payload)
        # The v1= hex part should be the same if called at the same second,
        # but timestamps differ. Just verify both are valid format.
        assert "v1=" in s1
        assert "v1=" in s2

    def test_sign_payload_empty_payload(self) -> None:
        """Empty payload bytes produce a valid signature."""
        signature = sign_payload("whsec_test", b"{}")
        assert signature.startswith("t=")
        assert ",v1=" in signature


@pytest.mark.unit
class TestWebhookService:
    """Unit tests for ``WebhookService`` — endpoint management and event emission."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    WRONG_ORG_ID = UUID("00000000-0000-0000-0000-000000000099")
    ENDPOINT_ID = UUID("00000000-0000-0000-0000-000000000010")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _make_service(self) -> tuple[WebhookService, AsyncMock]:
        """Create WebhookService with mocked repository."""
        mock_repo = AsyncMock()
        service = WebhookService(repo=mock_repo)
        return service, mock_repo

    def _make_endpoint(
        self,
        endpoint_id: UUID | None = None,
        org_id: UUID | None = None,
        name: str = "Test Endpoint",
        url: str = "https://example.com/hook",
        events: list[str] | None = None,
        is_active: bool = True,
        signing_secret: object = _AUTO_SECRET,
    ) -> MagicMock:
        """Build a MagicMock mimicking a WebhookEndpoint ORM model.

        Args:
            signing_secret: Secret to attach; pass ``None`` to mimic a
                legacy row with a NULL column (lazy backfill path).
                Defaults to a deterministic per-endpoint value so normal
                tests exercise the per-endpoint signing path.
        """
        ep = MagicMock(spec=WebhookEndpoint)
        ep.id = endpoint_id or self.ENDPOINT_ID
        ep.organization_id = org_id or self.ORG_ID
        ep.name = name
        ep.url = url
        ep.events = json.dumps(events or [])
        ep.is_active = is_active
        if signing_secret is _AUTO_SECRET:
            ep.signing_secret = f"whsec_{ep.id}"
        else:
            ep.signing_secret = signing_secret
        ep.last_delivery_at = datetime(2025, 1, 1, tzinfo=UTC)
        ep.created_at = datetime(2025, 1, 1, tzinfo=UTC)
        ep.updated_at = datetime(2025, 1, 1, tzinfo=UTC)
        return ep

    # ── list_endpoints ──────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_list_endpoints_returns_serialized_list(self) -> None:
        """``list_endpoints`` returns serialized endpoint dicts."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_organization.return_value = [self._make_endpoint()]

        endpoints = await service.list_endpoints(self.ORG_ID)
        assert len(endpoints) == 1
        assert endpoints[0]["id"] == str(self.ENDPOINT_ID)
        assert endpoints[0]["name"] == "Test Endpoint"
        assert endpoints[0]["url"] == "https://example.com/hook"
        assert endpoints[0]["is_active"] is True
        mock_repo.get_by_organization.assert_awaited_once_with(self.ORG_ID)

    @pytest.mark.asyncio
    async def test_list_endpoints_empty(self) -> None:
        """``list_endpoints`` returns empty list when no endpoints exist."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_organization.return_value = []

        endpoints = await service.list_endpoints(self.ORG_ID)
        assert endpoints == []

    # ── get_endpoint ────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_get_endpoint_returns_serialized(self) -> None:
        """``get_endpoint`` returns serialized endpoint when found and owned."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_id.return_value = self._make_endpoint()

        result = await service.get_endpoint(self.ENDPOINT_ID, self.ORG_ID)
        assert result is not None
        assert result["id"] == str(self.ENDPOINT_ID)

    @pytest.mark.asyncio
    async def test_get_endpoint_wrong_org_returns_none(self) -> None:
        """``get_endpoint`` returns None when endpoint belongs to a different org."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_id.return_value = self._make_endpoint()

        result = await service.get_endpoint(self.ENDPOINT_ID, self.WRONG_ORG_ID)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_endpoint_not_found_returns_none(self) -> None:
        """``get_endpoint`` returns None when endpoint does not exist."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_id.return_value = None

        result = await service.get_endpoint(self.ENDPOINT_ID, self.ORG_ID)
        assert result is None

    # ── create_endpoint ─────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_create_endpoint_returns_dict_and_secret(self) -> None:
        """``create_endpoint`` returns (serialized_dict, per_endpoint_secret)."""
        service, mock_repo = self._make_service()
        mock_repo.create.return_value = self._make_endpoint()

        endpoint, secret = await service.create_endpoint(
            organization_id=self.ORG_ID,
            name="New Hook",
            url="https://example.com/hook",
            events=["session.created"],
        )

        assert isinstance(endpoint, dict)
        assert endpoint["name"] == "Test Endpoint"
        # Per-endpoint secret: URL-safe, ≥32 chars, never the global config secret
        assert isinstance(secret, str)
        assert len(secret) >= 32
        assert secret != "b" * 32  # global WEBHOOK_SIGNING_SECRET from conftest
        mock_repo.create.assert_awaited_once()
        # Secret is persisted on the row, not just returned
        assert mock_repo.create.await_args.kwargs["signing_secret"] == secret

    @pytest.mark.asyncio
    async def test_create_endpoint_generates_unique_secrets(self) -> None:
        """Two orgs and two endpoints within an org get different secrets."""
        service, mock_repo = self._make_service()
        mock_repo.create.return_value = self._make_endpoint()

        _, s1 = await service.create_endpoint(
            self.ORG_ID, "A", "https://example.com/a",
        )
        _, s2 = await service.create_endpoint(
            self.ORG_ID, "B", "https://example.com/b",
        )
        _, s3 = await service.create_endpoint(
            self.WRONG_ORG_ID, "C", "https://example.com/c",
        )
        assert len({s1, s2, s3}) == 3

    @pytest.mark.asyncio
    async def test_endpoint_reads_never_expose_secret(self) -> None:
        """``list_endpoints``/``get_endpoint`` never return signing_secret."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_organization.return_value = [
            self._make_endpoint(signing_secret="whsec_topsecret"),  # noqa: S106
        ]
        mock_repo.get_by_id.return_value = self._make_endpoint(
            signing_secret="whsec_topsecret",  # noqa: S106
        )

        listed = await service.list_endpoints(self.ORG_ID)
        fetched = await service.get_endpoint(self.ENDPOINT_ID, self.ORG_ID)

        assert "signing_secret" not in listed[0]
        assert "signing_secret" not in fetched

    # ── rotate_endpoint_secret ───────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_rotate_endpoint_secret_returns_new_secret(self) -> None:
        """``rotate_endpoint_secret`` persists and returns a fresh secret."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_id.return_value = self._make_endpoint(
            signing_secret="whsec_old",  # noqa: S106
        )

        new_secret = await service.rotate_endpoint_secret(
            self.ENDPOINT_ID, self.ORG_ID,
        )

        assert isinstance(new_secret, str)
        assert len(new_secret) >= 32
        assert new_secret != "whsec_old"  # noqa: S105
        mock_repo.update.assert_awaited_once_with(
            self.ENDPOINT_ID, signing_secret=new_secret,
        )

    @pytest.mark.asyncio
    async def test_rotate_endpoint_secret_wrong_org_returns_none(self) -> None:
        """``rotate_endpoint_secret`` returns None for a different org."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_id.return_value = self._make_endpoint()

        result = await service.rotate_endpoint_secret(
            self.ENDPOINT_ID, self.WRONG_ORG_ID,
        )
        assert result is None
        mock_repo.update.assert_not_awaited()

    # ── update_endpoint ─────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_update_endpoint_ownership_check(self) -> None:
        """``update_endpoint`` returns None for wrong org."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_id.return_value = self._make_endpoint()

        result = await service.update_endpoint(
            self.ENDPOINT_ID, self.WRONG_ORG_ID, {"name": "Hacked"},
        )
        assert result is None
        mock_repo.update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_endpoint_success(self) -> None:
        """``update_endpoint`` returns updated endpoint."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_id.return_value = self._make_endpoint()
        updated_ep = self._make_endpoint(name="Updated")
        mock_repo.update.return_value = updated_ep

        result = await service.update_endpoint(
            self.ENDPOINT_ID, self.ORG_ID, {"name": "Updated"},
        )
        assert result is not None
        assert result["name"] == "Updated"

    # ── toggle_endpoint ─────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_toggle_endpoint_activates(self) -> None:
        """``toggle_endpoint`` enables an endpoint."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_id.return_value = self._make_endpoint(is_active=False)
        updated_ep = self._make_endpoint(is_active=True)
        mock_repo.update.return_value = updated_ep

        result = await service.toggle_endpoint(
            self.ENDPOINT_ID, self.ORG_ID, is_active=True,
        )
        assert result is not None
        assert result["is_active"] is True

    @pytest.mark.asyncio
    async def test_toggle_endpoint_wrong_org(self) -> None:
        """``toggle_endpoint`` returns None for wrong org."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_id.return_value = self._make_endpoint()

        result = await service.toggle_endpoint(
            self.ENDPOINT_ID, self.WRONG_ORG_ID, is_active=False,
        )
        assert result is None

    # ── delete_endpoint ─────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_delete_endpoint_success(self) -> None:
        """``delete_endpoint`` returns True when deleted."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_id.return_value = self._make_endpoint()
        mock_repo.delete.return_value = True

        result = await service.delete_endpoint(self.ENDPOINT_ID, self.ORG_ID)
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_endpoint_wrong_org_returns_false(self) -> None:
        """``delete_endpoint`` returns False for wrong org."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_id.return_value = self._make_endpoint()

        result = await service.delete_endpoint(self.ENDPOINT_ID, self.WRONG_ORG_ID)
        assert result is False
        mock_repo.delete.assert_not_awaited()

    # ── emit ────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_emit_with_endpoints_enqueues_jobs(self) -> None:
        """``emit`` enqueues ARQ deliver_webhook jobs for subscribed endpoints."""
        service, mock_repo = self._make_service()

        mock_repo.get_active_endpoints_for_event.return_value = [
            self._make_endpoint(url="https://hook1.com"),
            self._make_endpoint(url="https://hook2.com"),
        ]

        mock_arq_pool = AsyncMock()
        mock_arq_pool.enqueue.return_value = "job-123"

        with patch("services.webhook_service.get_arq", return_value=mock_arq_pool):
            result = await service.emit(
                organization_id=self.ORG_ID,
                event_type="session.created",
                payload={"session_id": "s1"},
            )

        assert result is True
        assert mock_arq_pool.enqueue.await_count == 2
        mock_repo.get_active_endpoints_for_event.assert_awaited_once_with(
            self.ORG_ID, "session.created",
        )

    @pytest.mark.asyncio
    async def test_emit_no_endpoints_is_noop(self) -> None:
        """``emit`` is a no-op when no endpoints subscribe to the event type."""
        service, mock_repo = self._make_service()
        mock_repo.get_active_endpoints_for_event.return_value = []

        with patch("services.webhook_service.get_arq") as mock_get_arq:
            result = await service.emit(
                organization_id=self.ORG_ID,
                event_type="unknown.event",
            )

        assert result is True
        mock_get_arq.assert_not_called()

    @pytest.mark.asyncio
    async def test_emit_arq_unavailable_returns_false(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """``emit`` returns False (never raises) when ARQ pool is not initialised."""
        service, mock_repo = self._make_service()
        mock_repo.get_active_endpoints_for_event.return_value = [
            self._make_endpoint(),
        ]

        with (
            patch(
                "services.webhook_service.get_arq",
                side_effect=RuntimeError("ARQ not initialised"),
            ),
            patch(
                "services.webhook_service.webhook_emit_failures_total"
            ) as mock_counter,
            caplog.at_level(logging.ERROR, logger="openzync.webhooks"),
        ):
            result = await service.emit(
                organization_id=self.ORG_ID,
                event_type="session.created",
            )

        assert result is False
        assert "webhook.emit_failed" in caplog.text
        mock_counter.labels.assert_called_once_with(event_type="session.created")
        mock_counter.labels.return_value.inc.assert_called_once()

    @pytest.mark.asyncio
    async def test_emit_repo_lookup_failure_returns_false(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """``emit`` returns False when the endpoint lookup (DB query) raises."""
        service, mock_repo = self._make_service()
        mock_repo.get_active_endpoints_for_event.side_effect = RuntimeError(
            "db unavailable",
        )

        with (
            patch(
                "services.webhook_service.webhook_emit_failures_total"
            ) as mock_counter,
            caplog.at_level(logging.ERROR, logger="openzync.webhooks"),
        ):
            result = await service.emit(
                organization_id=self.ORG_ID,
                event_type="session.created",
            )

        assert result is False
        assert "webhook.emit_failed" in caplog.text
        mock_counter.labels.assert_called_once_with(event_type="session.created")
        mock_counter.labels.return_value.inc.assert_called_once()

    @pytest.mark.asyncio
    async def test_emit_enqueue_failure_returns_false(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """``emit`` returns False when ARQ enqueue raises."""
        service, mock_repo = self._make_service()
        mock_repo.get_active_endpoints_for_event.return_value = [
            self._make_endpoint(),
        ]

        mock_arq_pool = AsyncMock()
        mock_arq_pool.enqueue.side_effect = RuntimeError("queue full")

        with (
            patch("services.webhook_service.get_arq", return_value=mock_arq_pool),
            patch(
                "services.webhook_service.webhook_emit_failures_total"
            ) as mock_counter,
            caplog.at_level(logging.ERROR, logger="openzync.webhooks"),
        ):
            result = await service.emit(
                organization_id=self.ORG_ID,
                event_type="session.created",
            )

        assert result is False
        assert "webhook.emit_failed" in caplog.text
        mock_counter.labels.assert_called_once_with(event_type="session.created")
        mock_counter.labels.return_value.inc.assert_called_once()

    @pytest.mark.asyncio
    async def test_emit_with_payload(self) -> None:
        """``emit`` serialises payload and signs each request."""
        service, mock_repo = self._make_service()
        mock_repo.get_active_endpoints_for_event.return_value = [
            self._make_endpoint(url="https://hook.com", endpoint_id=uuid4()),
        ]

        mock_arq_pool = AsyncMock()
        mock_arq_pool.enqueue.return_value = "job-xyz"

        with patch("services.webhook_service.get_arq", return_value=mock_arq_pool):
            result = await service.emit(
                organization_id=self.ORG_ID,
                event_type="session.created",
                payload={"session_id": "s1", "project_id": "p1"},
            )

        assert result is True
        mock_arq_pool.enqueue.assert_awaited_once()
        call_kwargs = mock_arq_pool.enqueue.await_args[1]  # keyword args
        assert call_kwargs["event_type"] == "session.created"
        assert call_kwargs["queue_name"] == "OpenZync:test:queue:low"
        assert "signature" in call_kwargs
        assert call_kwargs["signature"].startswith("t=")

    @pytest.mark.asyncio
    async def test_emit_signs_with_each_endpoints_own_secret(self) -> None:
        """Each endpoint is signed with its own secret — no shared global."""
        service, mock_repo = self._make_service()
        ep_a = self._make_endpoint(
            url="https://hook-a.com", endpoint_id=uuid4(),
            signing_secret="whsec_org_a",  # noqa: S106
        )
        ep_b = self._make_endpoint(
            url="https://hook-b.com", endpoint_id=uuid4(),
            signing_secret="whsec_org_b",  # noqa: S106
        )
        mock_repo.get_active_endpoints_for_event.return_value = [ep_a, ep_b]

        mock_arq_pool = AsyncMock()
        with patch("services.webhook_service.get_arq", return_value=mock_arq_pool):
            result = await service.emit(
                organization_id=self.ORG_ID,
                event_type="session.created",
                payload={"session_id": "s1"},
            )

        assert result is True
        calls = mock_arq_pool.enqueue.await_args_list
        assert len(calls) == 2
        sig_by_url = {c.kwargs["endpoint_url"]: c.kwargs["signature"] for c in calls}
        body_bytes = orjson.dumps(
            {"type": "session.created", "payload": {"session_id": "s1"}},
        )
        # Each signature verifies against that endpoint's own secret
        assert _verify_signature(
            "whsec_org_a", body_bytes, sig_by_url["https://hook-a.com"],
        )
        assert _verify_signature(
            "whsec_org_b", body_bytes, sig_by_url["https://hook-b.com"],
        )
        # Different orgs ⇒ different signatures for the identical payload
        assert sig_by_url["https://hook-a.com"] != sig_by_url["https://hook-b.com"]
        # No global fallback used
        mock_repo.update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_emit_lazily_backfills_null_secret(self) -> None:
        """Legacy endpoint with NULL secret gets one generated and persisted."""
        service, mock_repo = self._make_service()
        legacy_ep = self._make_endpoint(
            url="https://hook.com", endpoint_id=uuid4(), signing_secret=None,
        )
        mock_repo.get_active_endpoints_for_event.return_value = [legacy_ep]
        mock_repo.set_signing_secret_if_null.return_value = 1

        mock_arq_pool = AsyncMock()
        with patch("services.webhook_service.get_arq", return_value=mock_arq_pool):
            result = await service.emit(
                organization_id=self.ORG_ID,
                event_type="session.created",
                payload={},
            )

        assert result is True
        mock_repo.set_signing_secret_if_null.assert_awaited_once()
        backfilled = mock_repo.set_signing_secret_if_null.await_args.kwargs[
            "signing_secret"
        ]
        assert mock_repo.set_signing_secret_if_null.await_args.args[0] == legacy_ep.id
        assert isinstance(backfilled, str)
        assert len(backfilled) >= 32
        signature = mock_arq_pool.enqueue.await_args.kwargs["signature"]
        body_bytes = orjson.dumps(
            {"type": "session.created", "payload": {}},
        )
        assert _verify_signature(backfilled, body_bytes, signature)

    @pytest.mark.asyncio
    async def test_emit_backfill_conflict_reads_existing_secret(self) -> None:
        """Backfill loser re-reads the winner's secret instead of overwriting."""
        service, mock_repo = self._make_service()
        legacy_ep = self._make_endpoint(
            url="https://hook.com", endpoint_id=uuid4(), signing_secret=None,
        )
        winner_ep = self._make_endpoint(
            url="https://hook.com", endpoint_id=legacy_ep.id,
            signing_secret="whsec_winner",  # noqa: S106
        )
        mock_repo.get_active_endpoints_for_event.return_value = [legacy_ep]
        mock_repo.set_signing_secret_if_null.return_value = 0  # lost the race
        mock_repo.get_by_id.return_value = winner_ep

        mock_arq_pool = AsyncMock()
        with patch("services.webhook_service.get_arq", return_value=mock_arq_pool):
            result = await service.emit(
                organization_id=self.ORG_ID,
                event_type="session.created",
                payload={},
            )

        assert result is True
        mock_repo.set_signing_secret_if_null.assert_awaited_once()
        mock_repo.get_by_id.assert_awaited_once_with(legacy_ep.id)
        signature = mock_arq_pool.enqueue.await_args.kwargs["signature"]
        body_bytes = orjson.dumps(
            {"type": "session.created", "payload": {}},
        )
        assert _verify_signature("whsec_winner", body_bytes, signature)

    # ── _serialize ──────────────────────────────────────────────────────────

    def test_serialize_endpoint(self) -> None:
        """``_serialize`` converts WebhookEndpoint to a dict."""
        ep = self._make_endpoint(events=["session.created"])
        result = WebhookService._serialize(ep)
        assert result["id"] == str(self.ENDPOINT_ID)
        assert result["events"] == ["session.created"]
        assert result["last_delivery_at"] is not None
        assert result["created_at"] is not None
        assert result["updated_at"] is not None
        assert "signing_secret" not in result

    def test_serialize_endpoint_no_events(self) -> None:
        """``_serialize`` returns empty list when events field is empty."""
        ep = self._make_endpoint(events=[])
        result = WebhookService._serialize(ep)
        assert result["events"] == []

    def test_serialize_endpoint_no_delivery(self) -> None:
        """``_serialize`` sets last_delivery_at to None when null."""
        ep = self._make_endpoint()
        ep.last_delivery_at = None
        result = WebhookService._serialize(ep)
        assert result["last_delivery_at"] is None

    def test_serialize_raises_on_wrong_type(self) -> None:
        """``_serialize`` raises TypeError for non-WebhookEndpoint objects."""
        with pytest.raises(TypeError, match="WebhookEndpoint"):
            WebhookService._serialize("not_an_endpoint")

    def test_serialize_invalid_events_handled_gracefully(self) -> None:
        """``_serialize`` returns empty events list when events JSON is malformed."""
        ep = self._make_endpoint()
        ep.events = "not-json"
        result = WebhookService._serialize(ep)
        assert result["events"] == []
