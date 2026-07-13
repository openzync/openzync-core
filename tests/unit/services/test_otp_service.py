"""Unit tests for the OTP service and verification throttle.

Tests use mocked Redis and EmailService — no real infrastructure required.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.exceptions import RateLimitError, ValidationError
from middleware.auth_throttle import AuthThrottle
from services.otp_service import OtpService


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures — shared across OtpService tests
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_redis() -> AsyncMock:
    """Create a mock async Redis client.

    ``pipeline()`` is called **synchronously** (no ``await``) and returns a
    pipeline object whose methods (``setex``, ``incr``, ``execute``) are all
    async.  We use a plain ``MagicMock`` for ``pipeline`` so that calling
    ``redis.pipeline()`` returns an ``AsyncMock`` immediately — not a coroutine.
    """
    redis = AsyncMock()
    redis.pipeline = MagicMock(return_value=AsyncMock())
    return redis


@pytest.fixture
def mock_email_service() -> MagicMock:
    """Create a mock email service whose ``send_email`` is awaitable."""
    svc = MagicMock()
    svc.send_email = AsyncMock()
    return svc


@pytest.fixture
def otp_service(
    mock_redis: AsyncMock,
    mock_email_service: MagicMock,
) -> OtpService:
    """Create an ``OtpService`` with mocked Redis and email dependencies."""
    return OtpService(redis=mock_redis, email_service=mock_email_service)


# ═══════════════════════════════════════════════════════════════════════════════
# TestOtpService
# ═══════════════════════════════════════════════════════════════════════════════


class TestOtpService:
    """Unit tests for the ``OtpService`` class — OTP generation, hashing,
    storage, verification, rate limits, and invalidation."""

    # ── Generation & hashing ──────────────────────────────────────────────

    async def test_generate_otp_length(self, otp_service: OtpService) -> None:
        """Generated OTP should be exactly 6 digits."""
        otp = otp_service._generate_otp()
        assert len(otp) == 6
        assert otp.isdigit()

    async def test_hash_otp_deterministic(self, otp_service: OtpService) -> None:
        """Same input should produce the same SHA-256 hash."""
        h1 = otp_service._hash_otp("123456")
        h2 = otp_service._hash_otp("123456")
        assert h1 == h2

    async def test_hash_otp_different(self, otp_service: OtpService) -> None:
        """Different inputs should produce different hashes."""
        h1 = otp_service._hash_otp("123456")
        h2 = otp_service._hash_otp("654321")
        assert h1 != h2

    async def test_constant_time_comparison_used(
        self, otp_service: OtpService,
    ) -> None:
        """The verify method uses ``hmac.compare_digest`` for comparison."""
        source = inspect.getsource(otp_service.verify)
        assert "hmac.compare_digest" in source

    # ── Generate & send ───────────────────────────────────────────────────

    @patch("services.email_service.render_email_template", new_callable=AsyncMock)
    @patch("services.email_service.render_text_template", new_callable=AsyncMock)
    async def test_generate_and_send_stores_hash(
        self,
        mock_text: AsyncMock,
        mock_html: AsyncMock,
        otp_service: OtpService,
        mock_redis: AsyncMock,
        mock_email_service: MagicMock,
    ) -> None:
        """``generate_and_send`` stores OTP hash in Redis and sends an email."""
        # No cooldown active for any purpose
        mock_redis.exists.side_effect = [0, 0, 0, 0]
        # Send-count key does not exist (no rate limit)
        mock_redis.get.return_value = None

        # Stub template renders
        mock_html.return_value = "<html>code</html>"
        mock_text.return_value = "code"

        await otp_service.generate_and_send(
            email="test@example.com",
            purpose="signup",
        )

        pipeline = mock_redis.pipeline.return_value
        # 3 setex calls (hash, attempts, cooldown)
        assert pipeline.setex.await_count >= 3
        # 1 incr call (hourly send count)
        assert pipeline.incr.await_count >= 1
        # pipeline executed
        pipeline.execute.assert_awaited_once()

        # Email was dispatched
        mock_email_service.send_email.assert_awaited_once_with(
            to="test@example.com",
            subject="Your OpenZync verification code",
            html_body="<html>code</html>",
            text_body="code",
        )

    # ── Verify ────────────────────────────────────────────────────────────

    async def test_verify_correct_code(
        self,
        otp_service: OtpService,
        mock_redis: AsyncMock,
    ) -> None:
        """Verifying with the correct code returns ``True`` and deletes keys."""
        code = "123456"
        code_hash = otp_service._hash_otp(code)
        mock_redis.get.return_value = code_hash
        mock_redis.incr.return_value = 1  # first attempt

        result = await otp_service.verify(
            email="test@example.com",
            purpose="signup",
            code=code,
        )
        assert result is True
        # Keys deleted on successful verification
        mock_redis.delete.assert_awaited_once()

    async def test_verify_wrong_code(
        self,
        otp_service: OtpService,
        mock_redis: AsyncMock,
    ) -> None:
        """Verifying with a wrong code returns ``False`` without deleting keys."""
        correct_hash = otp_service._hash_otp("123456")
        mock_redis.get.return_value = correct_hash
        mock_redis.incr.return_value = 1

        result = await otp_service.verify(
            email="test@example.com",
            purpose="signup",
            code="000000",  # wrong code
        )
        assert result is False
        mock_redis.delete.assert_not_awaited()

    async def test_verify_expired(
        self,
        otp_service: OtpService,
        mock_redis: AsyncMock,
    ) -> None:
        """Verifying with no stored hash returns ``False``."""
        mock_redis.get.return_value = None

        result = await otp_service.verify(
            email="test@example.com",
            purpose="signup",
            code="123456",
        )
        assert result is False

    async def test_verify_max_attempts(
        self,
        otp_service: OtpService,
        mock_redis: AsyncMock,
    ) -> None:
        """Exceeding max attempts raises ``ValidationError`` and deletes keys."""
        correct_hash = otp_service._hash_otp("123456")
        mock_redis.get.return_value = correct_hash
        mock_redis.incr.return_value = 6  # exceeds _MAX_ATTEMPTS (5)

        with pytest.raises(ValidationError, match="Too many failed attempts"):
            await otp_service.verify(
                email="test@example.com",
                purpose="signup",
                code="123456",
            )
        # Keys deleted on max-attempts exhaustion
        mock_redis.delete.assert_awaited_once()

    # ── Rate limits ───────────────────────────────────────────────────────

    async def test_rate_limit_cooldown(
        self,
        otp_service: OtpService,
        mock_redis: AsyncMock,
        mock_email_service: MagicMock,
    ) -> None:
        """Active cooldown raises ``RateLimitError`` — email is not sent."""
        # ``_check_rate_limits`` iterates 4 purposes; return 1 on the second
        # iteration (password_reset cooldown active).
        mock_redis.exists.side_effect = [0, 1]

        with pytest.raises(RateLimitError, match="wait before requesting"):
            await otp_service.generate_and_send(
                email="test@example.com",
                purpose="signup",
            )

        mock_email_service.send_email.assert_not_awaited()

    async def test_rate_limit_max_sends(
        self,
        otp_service: OtpService,
        mock_redis: AsyncMock,
        mock_email_service: MagicMock,
    ) -> None:
        """Exceeding hourly send cap raises ``RateLimitError``."""
        mock_redis.exists.side_effect = [0, 0, 0, 0]  # no cooldowns
        mock_redis.get.return_value = 5  # send count at max (== _MAX_SENDS_PER_HOUR)

        with pytest.raises(RateLimitError, match="maximum number of OTP requests"):
            await otp_service.generate_and_send(
                email="test@example.com",
                purpose="signup",
            )

        mock_email_service.send_email.assert_not_awaited()

    # ── Invalidate ────────────────────────────────────────────────────────

    async def test_invalidate_deletes_keys(
        self,
        otp_service: OtpService,
        mock_redis: AsyncMock,
    ) -> None:
        """``invalidate`` deletes hash, attempts, and cooldown keys."""
        await otp_service.invalidate(email="test@example.com", purpose="signup")
        mock_redis.delete.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════════
# TestAuthThrottle
# ═══════════════════════════════════════════════════════════════════════════════


class TestAuthThrottle:
    """Unit tests for the ``AuthThrottle`` verify-attempt rate limiter."""

    @pytest.fixture
    def mock_redis(self) -> AsyncMock:
        """Create a fresh mock Redis for throttle tests."""
        return AsyncMock()

    async def test_check_verify_attempt_allows(
        self,
        mock_redis: AsyncMock,
    ) -> None:
        """Under-limit attempts pass without error."""
        mock_redis.incr.return_value = 1
        throttle = AuthThrottle(redis=mock_redis)

        await throttle.check_verify_attempt(email="test@example.com", ip="1.2.3.4")
        # No exception raised = success

    async def test_check_verify_attempt_blocks_email(
        self,
        mock_redis: AsyncMock,
    ) -> None:
        """Exceeding email threshold (10) raises ``RateLimitError``."""
        mock_redis.incr.return_value = 11
        throttle = AuthThrottle(redis=mock_redis)

        with pytest.raises(RateLimitError, match="Too many verification attempts"):
            await throttle.check_verify_attempt(
                email="test@example.com",
                ip="1.2.3.4",
            )

    async def test_check_verify_attempt_blocks_ip(
        self,
        mock_redis: AsyncMock,
    ) -> None:
        """Exceeding IP threshold (20) raises ``RateLimitError``."""
        # First ``incr`` call = email check (under limit), second = IP check (over)
        mock_redis.incr.side_effect = [5, 21]
        throttle = AuthThrottle(redis=mock_redis)

        with pytest.raises(RateLimitError, match="Too many verification attempts"):
            await throttle.check_verify_attempt(
                email="test@example.com",
                ip="1.2.3.4",
            )
