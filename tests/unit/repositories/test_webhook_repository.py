"""Unit tests for WebhookRepository — webhook endpoint CRUD and event filtering."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import orjson
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.webhook_repository import WebhookRepository


pytestmark = pytest.mark.unit


class TestWebhookRepository:
    """WebhookRepository unit tests."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    ENDPOINT_ID = UUID("00000000-0000-0000-0000-000000000010")

    @pytest.fixture
    def mock_db(self) -> AsyncMock:
        return AsyncMock(spec=AsyncSession)

    @pytest.fixture
    def repo(self, mock_db: AsyncMock) -> WebhookRepository:
        return WebhookRepository(db=mock_db)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _mock_endpoint(self, **overrides: object) -> MagicMock:
        ep = MagicMock()
        ep.id = overrides.get("id", self.ENDPOINT_ID)
        ep.organization_id = overrides.get("organization_id", self.ORG_ID)
        ep.name = overrides.get("name", "Test Webhook")
        ep.url = overrides.get("url", "https://example.com/hook")
        ep.events = overrides.get(
            "events", orjson.dumps(["message.created", "session.closed"]).decode()
        )
        ep.is_active = overrides.get("is_active", True)
        ep.last_delivery_at = overrides.get("last_delivery_at", None)
        ep.created_at = overrides.get("created_at", None)
        return ep

    # ── get_by_id ──────────────────────────────────────────────────────────────

    async def test_get_by_id_found(
        self, repo: WebhookRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_id returns endpoint when found."""
        endpoint = self._mock_endpoint()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = endpoint
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_id(endpoint_id=self.ENDPOINT_ID)

        assert result == endpoint

    async def test_get_by_id_not_found(
        self, repo: WebhookRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_id returns None when not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_id(endpoint_id=self.ENDPOINT_ID)

        assert result is None

    # ── get_by_organization ────────────────────────────────────────────────────

    async def test_get_by_organization(
        self, repo: WebhookRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_organization returns all endpoints for an org."""
        endpoints = [self._mock_endpoint(), self._mock_endpoint(id=uuid4())]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = endpoints
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_organization(organization_id=self.ORG_ID)

        assert result == endpoints

    async def test_get_by_organization_empty(
        self, repo: WebhookRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_organization returns empty list."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_organization(organization_id=self.ORG_ID)

        assert result == []

    # ── create ─────────────────────────────────────────────────────────────────

    async def test_create(
        self, repo: WebhookRepository, mock_db: AsyncMock
    ) -> None:
        """create inserts and returns a new webhook endpoint."""
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.create(
            organization_id=self.ORG_ID,
            name="My Webhook",
            url="https://example.com/hook",
            events=["message.created"],
        )

        assert result is not None
        mock_db.add.assert_called_once()
        mock_db.flush.assert_awaited_once()
        mock_db.refresh.assert_awaited_once()

    async def test_create_without_events(
        self, repo: WebhookRepository, mock_db: AsyncMock
    ) -> None:
        """create works without specifying events."""
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.create(
            organization_id=self.ORG_ID,
            name="No Events",
            url="https://example.com/hook",
        )

        assert result is not None

    # ── update ─────────────────────────────────────────────────────────────────

    async def test_update(
        self, repo: WebhookRepository, mock_db: AsyncMock
    ) -> None:
        """update modifies endpoint fields."""
        endpoint = self._mock_endpoint()
        # get_by_id call after the update
        updated_endpoint = self._mock_endpoint(name="Updated")
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = endpoint
        mock_db.execute.return_value = mock_result

        # After the update, get_by_id is called again
        get_result = MagicMock()
        get_result.scalar_one_or_none.return_value = updated_endpoint

        async def execute_side_effect(*args: object, **kwargs: object) -> MagicMock:
            return get_result

        mock_db.execute.side_effect = execute_side_effect

        result = await repo.update(
            endpoint_id=self.ENDPOINT_ID, name="Updated"
        )

        assert result is not None
        mock_db.flush.assert_awaited_once()

    async def test_update_no_fields(
        self, repo: WebhookRepository, mock_db: AsyncMock
    ) -> None:
        """update with no valid fields returns current endpoint."""
        endpoint = self._mock_endpoint()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = endpoint
        mock_db.execute.return_value = mock_result

        result = await repo.update(endpoint_id=self.ENDPOINT_ID)

        assert result == endpoint

    async def test_update_not_found(
        self, repo: WebhookRepository, mock_db: AsyncMock
    ) -> None:
        """update returns None when endpoint not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.update(endpoint_id=self.ENDPOINT_ID, name="Nope")

        assert result is None

    async def test_update_events(
        self, repo: WebhookRepository, mock_db: AsyncMock
    ) -> None:
        """update serialises events list to JSON."""
        endpoint = self._mock_endpoint()
        updated_endpoint = self._mock_endpoint(
            events=orjson.dumps(["session.closed"])
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = endpoint
        mock_db.execute.return_value = mock_result

        get_result = MagicMock()
        get_result.scalar_one_or_none.return_value = updated_endpoint
        mock_db.execute.side_effect = None
        mock_db.execute.side_effect = [mock_result, get_result]

        result = await repo.update(
            endpoint_id=self.ENDPOINT_ID,
            events=["session.closed"],
        )

        assert result is not None
        mock_db.flush.assert_awaited_once()

    # ── delete ─────────────────────────────────────────────────────────────────

    async def test_delete(
        self, repo: WebhookRepository, mock_db: AsyncMock
    ) -> None:
        """delete removes an endpoint and returns True."""
        endpoint = self._mock_endpoint()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = endpoint
        mock_db.execute.return_value = mock_result

        result = await repo.delete(endpoint_id=self.ENDPOINT_ID)

        assert result is True
        mock_db.delete.assert_awaited_once_with(endpoint)
        mock_db.flush.assert_awaited_once()

    async def test_delete_not_found(
        self, repo: WebhookRepository, mock_db: AsyncMock
    ) -> None:
        """delete returns False when endpoint not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.delete(endpoint_id=self.ENDPOINT_ID)

        assert result is False
        mock_db.delete.assert_not_called()

    # ── get_active_endpoints_for_event ─────────────────────────────────────────

    async def test_get_active_endpoints_for_event(
        self, repo: WebhookRepository, mock_db: AsyncMock
    ) -> None:
        """get_active_endpoints_for_event filters by subscribed event."""
        matching = self._mock_endpoint(
            events=orjson.dumps(["message.created", "session.closed"]).decode()
        )
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [matching]
        mock_db.execute.return_value = mock_result

        result = await repo.get_active_endpoints_for_event(
            organization_id=self.ORG_ID,
            event_type="message.created",
        )

        assert len(result) == 1
        assert result[0] == matching

    async def test_get_active_endpoints_empty_list_subscribes_all(
        self, repo: WebhookRepository, mock_db: AsyncMock
    ) -> None:
        """Empty events list means subscribe to all events."""
        endpoint = self._mock_endpoint(events=orjson.dumps([]).decode())
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [endpoint]
        mock_db.execute.return_value = mock_result

        result = await repo.get_active_endpoints_for_event(
            organization_id=self.ORG_ID,
            event_type="any.event",
        )

        assert len(result) == 1

    async def test_get_active_endpoints_no_match(
        self, repo: WebhookRepository, mock_db: AsyncMock
    ) -> None:
        """Only active endpoints with matching event are returned."""
        wrong = self._mock_endpoint(
            events=orjson.dumps(["session.closed"]).decode(),
        )
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [wrong]
        mock_db.execute.return_value = mock_result

        result = await repo.get_active_endpoints_for_event(
            organization_id=self.ORG_ID,
            event_type="message.created",
        )

        assert result == []

    async def test_get_active_endpoints_malformed_events(
        self, repo: WebhookRepository, mock_db: AsyncMock
    ) -> None:
        """Malformed events JSON is treated as no match."""
        endpoint = self._mock_endpoint(events="not-json")
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [endpoint]
        mock_db.execute.return_value = mock_result

        result = await repo.get_active_endpoints_for_event(
            organization_id=self.ORG_ID,
            event_type="anything",
        )

        assert result == []
