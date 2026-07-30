"""Unit tests for ARQ worker tasks: deliver_webhook and write_audit_log.

Tests cover:
- ``deliver_webhook`` — HTTP delivery with retry logic (5xx/timeout/network)
- ``_log_delivery`` — self-contained DB session for delivery persistence
- ``write_audit_log`` — DB-backed audit log entry via AuditLogService
"""

from __future__ import annotations

from unittest.mock import ANY, AsyncMock, MagicMock, patch

import httpx
import orjson
import pytest
from arq import Retry

from services.worker.tasks.audit_log import write_audit_log
from services.worker.tasks.deliver_webhook import _log_delivery, deliver_webhook

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════════════════
# Test helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _make_response(status_code: int, text: str = "") -> MagicMock:
    """Build a minimal httpx.Response-like mock."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    return resp


def _make_async_client_mock(response: MagicMock) -> AsyncMock:
    """Build an async context manager mock for ``httpx.AsyncClient``.

    The mocked ``AsyncClient`` returns *response* from ``.post()``.
    """
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post = AsyncMock(return_value=response)
    return client


def _make_session_fixture() -> tuple[MagicMock, AsyncMock]:
    """Build the engine / session / session-factory mock chain.

    Returns:
        (mock_engine_factory, mock_session_factory) where calling
        ``mock_engine_factory(url, **kw)`` returns a MagicMock engine and
        calling ``mock_session_factory(engine)`` returns an async context
        manager whose ``__aenter__`` yields a usable AsyncMock session.
    """
    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()

    mock_session_obj = AsyncMock()
    mock_session_cm = AsyncMock()
    mock_session_cm.__aenter__.return_value = mock_session_obj
    mock_session_factory = MagicMock(return_value=mock_session_cm)

    return mock_engine, mock_session_obj, mock_session_factory


DEFAULT_CTX: dict = {"job_id": "test-job-1"}
ENDPOINT_ID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
ENDPOINT_URL = "https://example.com/webhook"
BODY = '{"event":"test"}'
EVENT_TYPE = "session.created"
SIGNATURE = "sha256=abcdef123456"
ORG_ID = "12345678-1234-5678-1234-567812345678"
ACTOR_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
RESOURCE_ID = "ffffffff-gggg-hhhh-iiii-jjjjjjjjjjjj"


# ═══════════════════════════════════════════════════════════════════════════════
# deliver_webhook
# ═══════════════════════════════════════════════════════════════════════════════


class TestDeliverWebhook:
    """``deliver_webhook`` — HTTP delivery with conditional retry."""

    # ── Success ────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_success_does_not_raise(self) -> None:
        """2xx response succeeds without raising Retry."""
        resp = _make_response(200)
        client = _make_async_client_mock(resp)

        with (
            patch("services.worker.tasks.deliver_webhook.httpx.AsyncClient",
                  return_value=client),
            patch("services.worker.tasks.deliver_webhook._log_delivery",
                  AsyncMock()) as mock_log,
        ):
            await deliver_webhook(
                DEFAULT_CTX,
                endpoint_id=ENDPOINT_ID,
                endpoint_url=ENDPOINT_URL,
                body=BODY,
                event_type=EVENT_TYPE,
                signature=SIGNATURE,
                attempt=0,
            )

        mock_log.assert_awaited_once_with(
            endpoint_id=ENDPOINT_ID,
            event_type=EVENT_TYPE,
            attempt=0,
            status_code=200,
            success=True,
            error=None,
        )

    @pytest.mark.asyncio
    async def test_success_sends_correct_headers(self) -> None:
        """POST is called with expected headers."""
        resp = _make_response(201)
        client = _make_async_client_mock(resp)

        with (
            patch("services.worker.tasks.deliver_webhook.httpx.AsyncClient",
                  return_value=client),
            patch("services.worker.tasks.deliver_webhook._log_delivery",
                  AsyncMock()),
        ):
            await deliver_webhook(
                DEFAULT_CTX,
                endpoint_id=ENDPOINT_ID,
                endpoint_url=ENDPOINT_URL,
                body=BODY,
                event_type=EVENT_TYPE,
                signature=SIGNATURE,
                attempt=2,
            )

        client.post.assert_awaited_once_with(
            ENDPOINT_URL,
            content=BODY,
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Signature": SIGNATURE,
                "X-Webhook-Attempt": "2",
                "X-Webhook-Event": EVENT_TYPE,
                "User-Agent": "OpenZync-Webhook/1.0",
            },
        )

    # ── 4xx — not retried ─────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_4xx_does_not_raise_retry(self) -> None:
        """4xx client error is logged as warning, not retried."""
        resp = _make_response(422, "Invalid payload")
        client = _make_async_client_mock(resp)

        with (
            patch("services.worker.tasks.deliver_webhook.httpx.AsyncClient",
                  return_value=client),
            patch("services.worker.tasks.deliver_webhook._log_delivery",
                  AsyncMock()) as mock_log,
            patch("services.worker.tasks.deliver_webhook.logger.warning") as mock_warn,
        ):
            await deliver_webhook(
                DEFAULT_CTX,
                endpoint_id=ENDPOINT_ID,
                endpoint_url=ENDPOINT_URL,
                body=BODY,
                event_type=EVENT_TYPE,
                signature=SIGNATURE,
                attempt=0,
            )

        mock_log.assert_awaited_once()
        kwargs = mock_log.call_args.kwargs
        assert kwargs["status_code"] == 422
        assert kwargs["success"] is False
        assert "HTTP 422" in kwargs["error"]
        mock_warn.assert_called_once()

    # ── 5xx — retried up to MAX_ATTEMPTS ──────────────────────────────────────

    @pytest.mark.asyncio
    async def test_5xx_raises_retry_when_attempts_remain(self) -> None:
        """5xx server error raises Retry with exponential backoff."""
        resp = _make_response(502)
        client = _make_async_client_mock(resp)

        with (
            patch("services.worker.tasks.deliver_webhook.httpx.AsyncClient",
                  return_value=client),
            patch("services.worker.tasks.deliver_webhook._log_delivery",
                  AsyncMock()),
            pytest.raises(Retry) as exc_info,
        ):
            await deliver_webhook(
                DEFAULT_CTX,
                endpoint_id=ENDPOINT_ID,
                endpoint_url=ENDPOINT_URL,
                body=BODY,
                event_type=EVENT_TYPE,
                signature=SIGNATURE,
                attempt=2,
            )

        # defer=2**2 → defer_score in milliseconds
        assert exc_info.value.defer_score == (2**2) * 1000

    @pytest.mark.asyncio
    async def test_5xx_final_attempt_does_not_retry(self) -> None:
        """5xx on the last attempt does not raise Retry."""
        resp = _make_response(503)
        client = _make_async_client_mock(resp)

        with (
            patch("services.worker.tasks.deliver_webhook.httpx.AsyncClient",
                  return_value=client),
            patch("services.worker.tasks.deliver_webhook._log_delivery",
                  AsyncMock()) as mock_log,
        ):
            # attempt=4 with MAX_ATTEMPTS=5 means the 5th attempt is final
            # (0-indexed, so attempt < 5 → retry; attempt >= 5 → no retry)
            await deliver_webhook(
                DEFAULT_CTX,
                endpoint_id=ENDPOINT_ID,
                endpoint_url=ENDPOINT_URL,
                body=BODY,
                event_type=EVENT_TYPE,
                signature=SIGNATURE,
                attempt=5,
            )

        mock_log.assert_awaited_once()
        kwargs = mock_log.call_args.kwargs
        assert kwargs["status_code"] == 503
        assert kwargs["success"] is False
        assert "final" in kwargs["error"]

    @pytest.mark.asyncio
    async def test_5xx_backoff_doubles_with_each_attempt(self) -> None:
        """Retry defer doubles with attempt number: 2^attempt."""
        resp = _make_response(500)
        client = _make_async_client_mock(resp)

        for attempt in range(3):
            with (
                patch("services.worker.tasks.deliver_webhook.httpx.AsyncClient",
                      return_value=client),
                patch("services.worker.tasks.deliver_webhook._log_delivery",
                      AsyncMock()),
                pytest.raises(Retry) as exc_info,
            ):
                await deliver_webhook(
                    DEFAULT_CTX,
                    endpoint_id=ENDPOINT_ID,
                    endpoint_url=ENDPOINT_URL,
                    body=BODY,
                    event_type=EVENT_TYPE,
                    signature=SIGNATURE,
                    attempt=attempt,
                )

            assert exc_info.value.defer_score == (2**attempt) * 1000

    # ── httpx.TimeoutException ─────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_timeout_raises_retry_when_attempts_remain(self) -> None:
        """TimeoutException raises Retry with exponential backoff."""
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post = AsyncMock(side_effect=httpx.TimeoutException("Timed out"))

        with (
            patch("services.worker.tasks.deliver_webhook.httpx.AsyncClient",
                  return_value=client),
            patch("services.worker.tasks.deliver_webhook._log_delivery",
                  AsyncMock()),
            pytest.raises(Retry) as exc_info,
        ):
            await deliver_webhook(
                DEFAULT_CTX,
                endpoint_id=ENDPOINT_ID,
                endpoint_url=ENDPOINT_URL,
                body=BODY,
                event_type=EVENT_TYPE,
                signature=SIGNATURE,
                attempt=0,
            )

        assert exc_info.value.defer_score == 1000  # 2**0 seconds = 1000 ms

    @pytest.mark.asyncio
    async def test_timeout_final_attempt_does_not_retry(self) -> None:
        """TimeoutException on the last attempt does not raise Retry."""
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post = AsyncMock(side_effect=httpx.TimeoutException("Timed out"))

        with (
            patch("services.worker.tasks.deliver_webhook.httpx.AsyncClient",
                  return_value=client),
            patch("services.worker.tasks.deliver_webhook._log_delivery",
                  AsyncMock()) as mock_log,
        ):
            await deliver_webhook(
                DEFAULT_CTX,
                endpoint_id=ENDPOINT_ID,
                endpoint_url=ENDPOINT_URL,
                body=BODY,
                event_type=EVENT_TYPE,
                signature=SIGNATURE,
                attempt=5,
            )

        mock_log.assert_awaited_once()
        kwargs = mock_log.call_args.kwargs
        assert kwargs["status_code"] is None
        assert kwargs["success"] is False
        assert "Timeout" in kwargs["error"]

    # ── httpx.RequestError (network errors) ───────────────────────────────────

    @pytest.mark.asyncio
    async def test_request_error_raises_retry_when_attempts_remain(self) -> None:
        """RequestError raises Retry with exponential backoff."""
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post = AsyncMock(side_effect=httpx.RequestError("Connection refused"))

        with (
            patch("services.worker.tasks.deliver_webhook.httpx.AsyncClient",
                  return_value=client),
            patch("services.worker.tasks.deliver_webhook._log_delivery",
                  AsyncMock()),
            pytest.raises(Retry) as exc_info,
        ):
            await deliver_webhook(
                DEFAULT_CTX,
                endpoint_id=ENDPOINT_ID,
                endpoint_url=ENDPOINT_URL,
                body=BODY,
                event_type=EVENT_TYPE,
                signature=SIGNATURE,
                attempt=1,
            )

        assert exc_info.value.defer_score == 2000  # 2**1 seconds = 2000 ms

    @pytest.mark.asyncio
    async def test_request_error_final_attempt_does_not_retry(self) -> None:
        """RequestError on the last attempt does not raise Retry."""
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post = AsyncMock(side_effect=httpx.RequestError("DNS failure"))

        with (
            patch("services.worker.tasks.deliver_webhook.httpx.AsyncClient",
                  return_value=client),
            patch("services.worker.tasks.deliver_webhook._log_delivery",
                  AsyncMock()) as mock_log,
        ):
            await deliver_webhook(
                DEFAULT_CTX,
                endpoint_id=ENDPOINT_ID,
                endpoint_url=ENDPOINT_URL,
                body=BODY,
                event_type=EVENT_TYPE,
                signature=SIGNATURE,
                attempt=5,
            )

        mock_log.assert_awaited_once()
        kwargs = mock_log.call_args.kwargs
        assert kwargs["status_code"] is None
        assert kwargs["success"] is False
        assert "Network error" in kwargs["error"]

    # ── _log_delivery is always called — even when Retry is raised ────────────

    @pytest.mark.asyncio
    async def test_log_delivery_called_before_retry_propagates(self) -> None:
        """_log_delivery is called in the finally block before Retry propagates."""
        resp = _make_response(502)
        client = _make_async_client_mock(resp)
        mock_log = AsyncMock()

        with (
            patch("services.worker.tasks.deliver_webhook.httpx.AsyncClient",
                  return_value=client),
            patch("services.worker.tasks.deliver_webhook._log_delivery",
                  mock_log),
            pytest.raises(Retry),
        ):
            await deliver_webhook(
                DEFAULT_CTX,
                endpoint_id=ENDPOINT_ID,
                endpoint_url=ENDPOINT_URL,
                body=BODY,
                event_type=EVENT_TYPE,
                signature=SIGNATURE,
                attempt=0,
            )

        mock_log.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════════
# _log_delivery
# ═══════════════════════════════════════════════════════════════════════════════


class TestLogDelivery:
    """``_log_delivery`` — self-contained DB persistence."""

    @pytest.mark.asyncio
    async def test_creates_engine_and_commits_log_entry(self) -> None:
        """Engine is initialised, session created, log inserted, engine disposed."""
        mock_engine, mock_session_obj, mock_session_factory = _make_session_fixture()

        mock_settings = MagicMock()
        mock_settings.DATABASE_URL = "postgresql+asyncpg://localhost:5432/test"

        with (
            patch("services.worker.tasks.deliver_webhook.get_settings",
                  return_value=mock_settings),
            patch("services.worker.tasks.deliver_webhook.init_db_engine",
                  return_value=mock_engine) as mock_init_engine,
            patch("services.worker.tasks.deliver_webhook.get_async_session",
                  return_value=mock_session_factory),
            patch("services.worker.tasks.deliver_webhook.WebhookDeliveryLog",
                  MagicMock()) as mock_log_cls,
        ):
            await _log_delivery(
                endpoint_id=ENDPOINT_ID,
                event_type=EVENT_TYPE,
                attempt=1,
                status_code=200,
                success=True,
                error=None,
            )

        mock_init_engine.assert_called_once_with(
            "postgresql+asyncpg://localhost:5432/test",
            pool_size=2,
            max_overflow=2,
        )
        mock_session_obj.add.assert_called_once()
        mock_session_obj.commit.assert_awaited_once()
        mock_engine.dispose.assert_awaited_once()
        # The model class was called with the right kwargs
        mock_log_cls.assert_called_once_with(
            endpoint_id=ANY,
            event_type=EVENT_TYPE,
            attempt=1,
            status_code=200,
            success=True,
            error=None,
        )

    @pytest.mark.asyncio
    async def test_logs_error_if_commit_fails(self) -> None:
        """Exception during session operation is caught and logged."""
        mock_engine, mock_session_obj, mock_session_factory = _make_session_fixture()
        mock_session_obj.commit.side_effect = Exception("DB is down")

        mock_settings = MagicMock()
        mock_settings.DATABASE_URL = "postgresql+asyncpg://localhost:5432/test"

        with (
            patch("services.worker.tasks.deliver_webhook.get_settings",
                  return_value=mock_settings),
            patch("services.worker.tasks.deliver_webhook.init_db_engine",
                  return_value=mock_engine),
            patch("services.worker.tasks.deliver_webhook.get_async_session",
                  return_value=mock_session_factory),
            patch("services.worker.tasks.deliver_webhook.WebhookDeliveryLog",
                  MagicMock()),
            patch("services.worker.tasks.deliver_webhook.logger.exception") as mock_log,
        ):
            await _log_delivery(
                endpoint_id=ENDPOINT_ID,
                event_type=EVENT_TYPE,
                attempt=0,
                status_code=None,
                success=False,
                error="Timeout: connection timed out",
            )

        mock_log.assert_called_once_with("Failed to persist webhook delivery log")
        # Engine must still be disposed even after failure
        mock_engine.dispose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_engine_disposed_on_exception(self) -> None:
        """Engine is disposed in finally block even if commit fails."""
        mock_engine, mock_session_obj, mock_session_factory = _make_session_fixture()
        mock_session_obj.commit.side_effect = RuntimeError("Commit failed")

        mock_settings = MagicMock()
        mock_settings.DATABASE_URL = "postgresql+asyncpg://localhost:5432/test"

        with (
            patch("services.worker.tasks.deliver_webhook.get_settings",
                  return_value=mock_settings),
            patch("services.worker.tasks.deliver_webhook.init_db_engine",
                  return_value=mock_engine),
            patch("services.worker.tasks.deliver_webhook.get_async_session",
                  return_value=mock_session_factory),
            patch("services.worker.tasks.deliver_webhook.WebhookDeliveryLog",
                  MagicMock()),
            patch("services.worker.tasks.deliver_webhook.logger.exception"),
        ):
            await _log_delivery(
                endpoint_id=ENDPOINT_ID,
                event_type=EVENT_TYPE,
                attempt=0,
                status_code=None,
                success=False,
                error="Timeout",
            )

        mock_engine.dispose.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════════
# write_audit_log
# ═══════════════════════════════════════════════════════════════════════════════


class TestWriteAuditLog:
    """``write_audit_log`` — self-contained audit log persistence."""

    @pytest.fixture(autouse=True)
    def _patch_deps(self) -> None:
        """Patch core dependencies for every write_audit_log test.

        These are applied to every test so we don't repeat the patch stack.
        Individual tests can override specific return values (e.g. the
        service mock) via their own patches atop these.
        """
        self._patchers = []

        # ------------------------------------------------------------------
        # Lazy imports inside write_audit_log():
        #   from core.config import settings
        #   from core.db import get_async_session, init_db_engine
        # ------------------------------------------------------------------

        # settings — goes through core.config.__getattr__ → get_settings()
        mock_settings = MagicMock()
        mock_settings.DATABASE_URL = "postgresql+asyncpg://localhost:5432/test"

        p = patch("core.config.get_settings", return_value=mock_settings)
        p.start()
        self._patchers.append(p)

        # Engine chain
        self._mock_engine = MagicMock()
        self._mock_engine.dispose = AsyncMock()

        p = patch("core.db.init_db_engine", return_value=self._mock_engine)
        p.start()
        self._patchers.append(p)

        self._mock_session_obj = AsyncMock()
        mock_session_cm = AsyncMock()
        mock_session_cm.__aenter__.return_value = self._mock_session_obj
        self._mock_session_factory = MagicMock(return_value=mock_session_cm)

        p = patch("core.db.get_async_session",
                  return_value=self._mock_session_factory)
        p.start()
        self._patchers.append(p)

        # AuditLogService
        self._mock_service = AsyncMock()
        p = patch(
            "services.worker.tasks.audit_log.AuditLogService",
            return_value=self._mock_service,
        )
        p.start()
        self._patchers.append(p)

        # structlog.contextvars
        self._mock_bind = MagicMock()
        p = patch(
            "services.worker.tasks.audit_log.structlog.contextvars.bind_contextvars",
            self._mock_bind,
        )
        p.start()
        self._patchers.append(p)

        yield

        for p in reversed(self._patchers):
            p.stop()

    # ── Success paths ─────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_basic_success_with_trace_id(self) -> None:
        """Audit log is written successfully when trace_id is provided."""
        await write_audit_log(
            DEFAULT_CTX,
            organization_id=ORG_ID,
            actor_id=ACTOR_ID,
            actor_type="user",
            action="session.create",
            resource_type="session",
            resource_id=RESOURCE_ID,
            details=orjson.dumps({"source": "api"}).decode(),
            ip_address="192.168.1.1",
            trace_id="trace-xyz-789",
        )

        self._mock_bind.assert_called_once_with(trace_id="trace-xyz-789")
        self._mock_service.log_action.assert_awaited_once_with(
            organization_id=ANY,
            actor_id=ACTOR_ID,
            actor_type="user",
            action="session.create",
            resource_type="session",
            resource_id=RESOURCE_ID,
            details={"source": "api"},
            ip_address="192.168.1.1",
        )
        self._mock_engine.dispose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_basic_success_without_trace_id(self) -> None:
        """Audit log is written successfully without trace_id."""
        await write_audit_log(
            DEFAULT_CTX,
            organization_id=ORG_ID,
            actor_id=ACTOR_ID,
            actor_type="user",
            action="session.create",
            resource_type="session",
            resource_id="sess-456",
            details=None,
            ip_address="192.168.1.1",
            trace_id="",
        )

        self._mock_bind.assert_not_called()
        self._mock_service.log_action.assert_awaited_once()
        self._mock_engine.dispose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_with_details_json_parsed(self) -> None:
        """JSON string details are parsed via orjson.loads."""
        details_json = orjson.dumps({"key": "value", "nested": {"a": 1}}).decode()

        await write_audit_log(
            DEFAULT_CTX,
            organization_id=ORG_ID,
            actor_id=ACTOR_ID,
            actor_type="user",
            action="resource.update",
            resource_type="resource",
            resource_id=RESOURCE_ID,
            details=details_json,
            ip_address=None,
            trace_id="",
        )

        call_kwargs = self._mock_service.log_action.call_args.kwargs
        assert call_kwargs["details"] == {"key": "value", "nested": {"a": 1}}

    @pytest.mark.asyncio
    async def test_without_details_uses_empty_dict(self) -> None:
        """When details is None or empty, parsed_details defaults to {}."""
        await write_audit_log(
            DEFAULT_CTX,
            organization_id=ORG_ID,
            actor_id=ACTOR_ID,
            actor_type="user",
            action="resource.update",
            resource_type="resource",
            resource_id=RESOURCE_ID,
            details=None,
            ip_address=None,
            trace_id="",
        )

        call_kwargs = self._mock_service.log_action.call_args.kwargs
        assert call_kwargs["details"] == {}

    @pytest.mark.asyncio
    async def test_without_optional_fields(self) -> None:
        """All optional fields default to None when not provided."""
        await write_audit_log(
            DEFAULT_CTX,
            action="session.create",
            resource_type="session",
        )

        self._mock_service.log_action.assert_awaited_once_with(
            organization_id=None,
            actor_id=None,
            actor_type=None,
            action="session.create",
            resource_type="session",
            resource_id=None,
            details={},
            ip_address=None,
        )

    # ── Error handling ─────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_exception_from_log_action_is_caught_and_re_raised(self) -> None:
        """Exception in service.log_action is caught, logged, and re-raised."""
        self._mock_service.log_action.side_effect = ValueError("invalid actor_type")

        with (
            patch("services.worker.tasks.audit_log.logger.exception") as mock_log,
            pytest.raises(ValueError, match="invalid actor_type"),
        ):
            await write_audit_log(
                DEFAULT_CTX,
                organization_id=ORG_ID,
                actor_id=ACTOR_ID,
                actor_type="invalid_type",
                action="session.create",
                resource_type="session",
                resource_id=RESOURCE_ID,
                details=None,
                ip_address=None,
                trace_id="",
            )

        mock_log.assert_called_once()
        call_args = mock_log.call_args
        assert call_args[0][0] == "audit_task.write_failed"
        assert call_args[1]["extra"]["action"] == "session.create"
        assert call_args[1]["extra"]["job_id"] == "test-job-1"

    @pytest.mark.asyncio
    async def test_engine_is_disposed_on_exception(self) -> None:
        """Engine.dispose() is called in the finally block even on failure."""
        self._mock_service.log_action.side_effect = RuntimeError("fail")

        with pytest.raises(RuntimeError):
            await write_audit_log(
                DEFAULT_CTX,
                action="session.create",
                resource_type="session",
            )

        self._mock_engine.dispose.assert_awaited_once()

    # ── Lazy import paths ──────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_lazy_imports_resolve_correctly(self) -> None:
        """Lazy imports (settings, init_db_engine, get_async_session) resolve.

        The function body contains ``from core.config import settings`` and
        ``from core.db import get_async_session, init_db_engine``.  Calling
        the function exercises these imports.  We verify the chain by
        checking that our patched ``init_db_engine`` was invoked with the
        DATABASE_URL value from our mock settings (proving the settings
        import resolved through ``__getattr__`` → ``get_settings()``).
        """
        await write_audit_log(
            DEFAULT_CTX,
            organization_id=ORG_ID,
            actor_id=ACTOR_ID,
            actor_type="user",
            action="session.create",
            resource_type="session",
            resource_id=RESOURCE_ID,
            details=None,
            ip_address="10.0.0.1",
            trace_id="trace-1",
        )

        import core.db
        actual_url = core.db.init_db_engine.call_args[0][0]
        assert "localhost:5432/test" in actual_url
        assert "+asyncpg" in actual_url
        # Engine disposed — full lifecycle completed
        core.db.init_db_engine.return_value.dispose.assert_awaited_once()
