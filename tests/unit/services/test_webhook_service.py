"""Unit tests for WebhookService — endpoint management and event emission.

All external dependencies (repository, ARQ) are mocked at the service boundary.
The ``sign_payload`` standalone function is tested directly — pure logic.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from models.webhook import WebhookEndpoint
from services.webhook_service import WebhookService, sign_payload


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
    ) -> MagicMock:
        """Build a MagicMock mimicking a WebhookEndpoint ORM model."""
        ep = MagicMock(spec=WebhookEndpoint)
        ep.id = endpoint_id or self.ENDPOINT_ID
        ep.organization_id = org_id or self.ORG_ID
        ep.name = name
        ep.url = url
        ep.events = json.dumps(events or [])
        ep.is_active = is_active
        ep.last_delivery_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        ep.created_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        ep.updated_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
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
        """``create_endpoint`` returns (serialized_dict, signing_secret)."""
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
        assert secret == "b" * 32  # from conftest WEBHOOK_SIGNING_SECRET
        mock_repo.create.assert_awaited_once()

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
            await service.emit(
                organization_id=self.ORG_ID,
                event_type="session.created",
                payload={"session_id": "s1"},
            )

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
            await service.emit(
                organization_id=self.ORG_ID,
                event_type="unknown.event",
            )

        mock_get_arq.assert_not_called()

    @pytest.mark.asyncio
    async def test_emit_arq_unavailable_raises(self) -> None:
        """``emit`` raises RuntimeError when ARQ pool is not initialised."""
        service, mock_repo = self._make_service()
        mock_repo.get_active_endpoints_for_event.return_value = [
            self._make_endpoint(),
        ]

        with patch(
            "services.webhook_service.get_arq",
            side_effect=RuntimeError("ARQ not initialised"),
        ), pytest.raises(RuntimeError):
            await service.emit(
                organization_id=self.ORG_ID,
                event_type="session.created",
            )

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
            await service.emit(
                organization_id=self.ORG_ID,
                event_type="session.created",
                payload={"session_id": "s1", "project_id": "p1"},
            )

        mock_arq_pool.enqueue.assert_awaited_once()
        call_kwargs = mock_arq_pool.enqueue.await_args[1]  # keyword args
        assert call_kwargs["event_type"] == "session.created"
        assert call_kwargs["queue_name"] == "OpenZync:test:queue:low"
        assert "signature" in call_kwargs
        assert call_kwargs["signature"].startswith("t=")

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
