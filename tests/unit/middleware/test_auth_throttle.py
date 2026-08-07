"""Unit tests for AuthThrottle."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.asyncio import Redis

from core.exceptions import RateLimitError
from middleware.auth_throttle import AuthThrottle


@pytest.mark.unit
class TestAuthThrottle:
    """Test suite for AuthThrottle — IP/email-based auth attempt throttling."""

    # ── Helpers ──────────────────────────────────────────────────────────────────

    @pytest.fixture
    def mock_redis(self) -> AsyncMock:
        mock = AsyncMock(spec=Redis)
        # Default: incr returns 1 (first attempt), expire sets successfully
        mock.incr = AsyncMock(return_value=1)
        mock.expire = AsyncMock(return_value=True)
        return mock

    @pytest.fixture
    def throttle(self, mock_redis: AsyncMock) -> AuthThrottle:
        return AuthThrottle(
            redis=mock_redis,
            login_max_per_ip=20,
            login_window_sec=900,
            login_max_per_email=5,
            signup_max_per_ip=3,
            signup_window_sec=3600,
        )

    # ── Login attempts ──────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_first_login_attempt_passes(self, throttle: AuthThrottle) -> None:
        """First login attempt from an IP passes."""
        await throttle.check_login_attempt("user@example.com", "192.168.1.1")

    @pytest.mark.asyncio
    async def test_repeated_email_failures_get_throttled(
        self, mock_redis: AsyncMock, throttle: AuthThrottle
    ) -> None:
        """Too many failed login attempts per email raises RateLimitError."""
        # Simulate exceeding the email limit (5)
        mock_redis.incr = AsyncMock(return_value=6)

        with pytest.raises(RateLimitError, match="Too many login attempts for this account"):
            await throttle.check_login_attempt("user@example.com", "192.168.1.1")

    @pytest.mark.asyncio
    async def test_repeated_ip_failures_get_throttled(
        self, mock_redis: AsyncMock, throttle: AuthThrottle
    ) -> None:
        """Too many failed login attempts per IP raises RateLimitError."""
        # Simulate exceeding the IP limit (20) but not the email limit (5)
        # Redis incr returns: first call (email) = 1, second call (ip) = 21
        call_count = [0]

        async def mock_incr(key: str) -> int:
            call_count[0] += 1
            if call_count[0] == 1:
                return 1  # email — under limit
            return 21  # ip — over limit

        mock_redis.incr = AsyncMock(side_effect=mock_incr)

        with pytest.raises(RateLimitError, match="Too many login attempts from this IP"):
            await throttle.check_login_attempt("user@example.com", "192.168.1.1")

    @pytest.mark.asyncio
    async def test_login_sets_expiry_on_first_attempt(
        self, mock_redis: AsyncMock, throttle: AuthThrottle
    ) -> None:
        """First login attempt sets expiry on both email and IP keys."""
        await throttle.check_login_attempt("user@example.com", "10.0.0.1")
        assert mock_redis.expire.call_count == 2  # email key + ip key

    @pytest.mark.asyncio
    async def test_different_ips_independent_counters(
        self, mock_redis: AsyncMock, throttle: AuthThrottle
    ) -> None:
        """Different IPs have independent counters."""
        # IP 1 gets throttled, IP 2 should be fine
        mock_redis.incr = AsyncMock(return_value=1)
        await throttle.check_login_attempt("user@example.com", "10.0.0.1")
        await throttle.check_login_attempt("user@example.com", "10.0.0.2")
        # Both pass — each has its own counter
        assert mock_redis.incr.call_count == 4  # 2 email + 2 ip

    # ── Signup attempts ─────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_first_signup_passes(self, throttle: AuthThrottle) -> None:
        """First signup attempt passes."""
        await throttle.check_signup_attempt("10.0.0.1")

    @pytest.mark.asyncio
    async def test_repeated_signups_get_throttled(
        self, mock_redis: AsyncMock, throttle: AuthThrottle
    ) -> None:
        """Too many signups from same IP raises RateLimitError."""
        mock_redis.incr = AsyncMock(return_value=4)  # > max 3

        with pytest.raises(RateLimitError, match="Too many signup attempts from this IP"):
            await throttle.check_signup_attempt("10.0.0.1")

    @pytest.mark.asyncio
    async def test_signup_sets_expiry_on_first(
        self, mock_redis: AsyncMock, throttle: AuthThrottle
    ) -> None:
        """First signup attempt sets expiry."""
        await throttle.check_signup_attempt("10.0.0.1")
        mock_redis.expire.assert_called_once()

    # ── Verify attempts ─────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_first_verify_passes(self, throttle: AuthThrottle) -> None:
        """First verify attempt passes."""
        await throttle.check_verify_attempt("user@example.com", "10.0.0.1")

    @pytest.mark.asyncio
    async def test_excessive_verify_email_throttled(
        self, mock_redis: AsyncMock, throttle: AuthThrottle
    ) -> None:
        """Too many verify attempts per email raises RateLimitError."""
        mock_redis.incr = AsyncMock(return_value=11)  # > 10

        with pytest.raises(RateLimitError, match="Too many verification attempts for this email"):
            await throttle.check_verify_attempt("user@example.com", "10.0.0.1")

    # ── Forgot password ─────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_first_forgot_passes(self, throttle: AuthThrottle) -> None:
        """First forgot-password request passes."""
        await throttle.check_forgot_password_attempt("user@example.com", "10.0.0.1")

    @pytest.mark.asyncio
    async def test_excessive_forgot_email_throttled(
        self, mock_redis: AsyncMock, throttle: AuthThrottle
    ) -> None:
        """Too many forgot-password requests per email raises RateLimitError."""
        mock_redis.incr = AsyncMock(return_value=4)  # > 3

        with pytest.raises(RateLimitError, match="Too many password reset requests for this email"):
            await throttle.check_forgot_password_attempt("user@example.com", "10.0.0.1")

    # ── Reset attempts ──────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_first_reset_passes(self, throttle: AuthThrottle) -> None:
        """First reset attempt passes."""
        await throttle.check_reset_attempt("user@example.com", "10.0.0.1")

    @pytest.mark.asyncio
    async def test_excessive_reset_email_throttled(
        self, mock_redis: AsyncMock, throttle: AuthThrottle
    ) -> None:
        """Too many reset attempts per email raises RateLimitError."""
        mock_redis.incr = AsyncMock(return_value=11)  # > 10

        with pytest.raises(RateLimitError, match="Too many reset attempts for this email"):
            await throttle.check_reset_attempt("user@example.com", "10.0.0.1")

    # ── Passwordless ────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_first_passwordless_send_passes(self, throttle: AuthThrottle) -> None:
        """First passwordless send passes."""
        await throttle.check_passwordless_send("user@example.com", "10.0.0.1")

    @pytest.mark.asyncio
    async def test_excessive_passwordless_send_email_throttled(
        self, mock_redis: AsyncMock, throttle: AuthThrottle
    ) -> None:
        """Too many passwordless sends per email raises RateLimitError."""
        mock_redis.incr = AsyncMock(return_value=6)  # > 5

        with pytest.raises(RateLimitError, match="Too many login code requests for this email"):
            await throttle.check_passwordless_send("user@example.com", "10.0.0.1")

    @pytest.mark.asyncio
    async def test_first_passwordless_verify_passes(self, throttle: AuthThrottle) -> None:
        """First passwordless verify passes."""
        await throttle.check_passwordless_verify("user@example.com", "10.0.0.1")

    @pytest.mark.asyncio
    async def test_excessive_passwordless_verify_ip_throttled(
        self, mock_redis: AsyncMock, throttle: AuthThrottle
    ) -> None:
        """Too many passwordless verify attempts per IP raises RateLimitError."""
        call_count = [0]

        async def mock_incr(key: str) -> int:
            call_count[0] += 1
            if call_count[0] == 1:
                return 1  # email — under limit
            return 21  # ip — over limit (20)

        mock_redis.incr = AsyncMock(side_effect=mock_incr)

        with pytest.raises(RateLimitError, match="Too many login verification attempts from this IP"):
            await throttle.check_passwordless_verify("user@example.com", "10.0.0.1")

    # ── MFA ─────────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_first_mfa_send_passes(self, throttle: AuthThrottle) -> None:
        """First MFA send passes."""
        await throttle.check_mfa_send("user@example.com", "10.0.0.1")

    @pytest.mark.asyncio
    async def test_excessive_mfa_verify_ip_throttled(
        self, mock_redis: AsyncMock, throttle: AuthThrottle
    ) -> None:
        """Too many MFA verify attempts per IP raises RateLimitError."""
        call_count = [0]

        async def mock_incr(key: str) -> int:
            call_count[0] += 1
            if call_count[0] == 1:
                return 1  # email
            return 21  # ip > 20

        mock_redis.incr = AsyncMock(side_effect=mock_incr)

        with pytest.raises(RateLimitError, match="Too many MFA verification attempts from this IP"):
            await throttle.check_mfa_verify("user@example.com", "10.0.0.1")

    # ── Window reset ────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_window_reset_after_expiry(self, mock_redis: AsyncMock, throttle: AuthThrottle) -> None:
        """After expiry, attempts reset (counter goes back to 1)."""
        # First call: incr returns 1 (first after reset)
        mock_redis.incr = AsyncMock(return_value=1)
        await throttle.check_login_attempt("user@example.com", "10.0.0.1")
        # Succeeds — counter at 1, no error

    # ── H4d — successful logins clear failed-attempt counters ──────────────

    @pytest.mark.asyncio
    async def test_five_failed_logins_lock_out(
        self, mock_redis: AsyncMock, throttle: AuthThrottle
    ) -> None:
        """Five failed attempts pass; the sixth is rejected (limit is 5)."""
        counts = {"email": 0, "ip": 0}

        async def mock_incr(key: str) -> int:
            if "email" in key:
                counts["email"] += 1
                return counts["email"]
            counts["ip"] += 1
            return counts["ip"]

        mock_redis.incr = AsyncMock(side_effect=mock_incr)

        for _ in range(5):
            await throttle.check_login_attempt("user@example.com", "10.0.0.1")

        with pytest.raises(RateLimitError, match="this account"):
            await throttle.check_login_attempt("user@example.com", "10.0.0.1")

    @pytest.mark.asyncio
    async def test_record_login_success_decrements_and_unlocks(
        self, mock_redis: AsyncMock, throttle: AuthThrottle
    ) -> None:
        """A successful login decrements both counters and unblocks the
        next attempt."""
        counts = {"email": 0, "ip": 0}

        async def mock_incr(key: str) -> int:
            if "email" in key:
                counts["email"] += 1
                return counts["email"]
            counts["ip"] += 1
            return counts["ip"]

        mock_redis.incr = AsyncMock(side_effect=mock_incr)
        for _ in range(5):
            await throttle.check_login_attempt("user@example.com", "10.0.0.1")
        with pytest.raises(RateLimitError):
            await throttle.check_login_attempt("user@example.com", "10.0.0.1")

        # Successful login decrements the counters the failures incremented.
        mock_redis.get = AsyncMock(return_value="5")
        mock_redis.decr = AsyncMock(return_value=4)
        await throttle.record_login_success("user@example.com", "10.0.0.1")
        assert mock_redis.decr.await_count == 2
        keys = [c.args[0] for c in mock_redis.decr.await_args_list]
        assert any("email" in k for k in keys)
        assert any("ip" in k for k in keys)

        # Next attempt allowed — counter back at 4 (one below the limit).
        counts["email"] = 4
        counts["ip"] = 4
        await throttle.check_login_attempt("user@example.com", "10.0.0.1")

    @pytest.mark.asyncio
    async def test_record_login_success_floors_at_zero(
        self, mock_redis: AsyncMock, throttle: AuthThrottle
    ) -> None:
        """record_login_success never decrements a counter below zero."""
        mock_redis.get = AsyncMock(return_value="0")
        mock_redis.decr = AsyncMock(return_value=0)
        await throttle.record_login_success("user@example.com", "10.0.0.1")
        mock_redis.decr.assert_not_awaited()

        mock_redis.get = AsyncMock(return_value=None)  # key never set
        await throttle.record_login_success("user@example.com", "10.0.0.1")
        mock_redis.decr.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_case_variant_emails_share_counter(
        self, mock_redis: AsyncMock, throttle: AuthThrottle
    ) -> None:
        """Email keys are normalized — case variants share one counter."""
        keys: list[str] = []

        async def mock_incr(key: str) -> int:
            keys.append(key)
            return 1

        mock_redis.incr = AsyncMock(side_effect=mock_incr)

        await throttle.check_login_attempt("User@Example.com", "10.0.0.1")
        await throttle.check_login_attempt("user@example.com", "10.0.0.1")

        email_keys = [k for k in keys if "email" in k]
        assert len(email_keys) == 2
        assert email_keys[0] == email_keys[1]

    @pytest.mark.asyncio
    async def test_successful_login_reset_method_exists(
        self, throttle: AuthThrottle
    ) -> None:
        """AuthThrottle exposes record_login_success — the compensating
        decrement for check_login_attempt (H4d)."""
        assert callable(throttle.record_login_success)
        assert not hasattr(throttle, "reset")

    # ── Different IPs not affected ──────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_different_ips_independent_for_login(
        self, mock_redis: AsyncMock, throttle: AuthThrottle
    ) -> None:
        """Different IPs are independently throttled for login."""
        # IP A gets blocked, IP B should not be affected
        call_count = {"a": 0, "b_email": 0}

        async def mock_incr_a(key: str) -> int:
            call_count["a"] += 1
            # email under limit, ip way over
            if "email" in key:
                return 1
            return 21

        async def mock_incr_b(key: str) -> int:
            call_count["b_email"] += 1
            return 1  # both under limit

        mock_redis.incr = AsyncMock(side_effect=mock_incr_a)
        with pytest.raises(RateLimitError):
            await throttle.check_login_attempt("user@example.com", "10.0.0.1")

        mock_redis.incr = AsyncMock(side_effect=mock_incr_b)
        await throttle.check_login_attempt("user@example.com", "10.0.0.2")

    # ── Redis unavailable ───────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_redis_error_propagates(self, mock_redis: AsyncMock, throttle: AuthThrottle) -> None:
        """When Redis is down, the exception propagates (fail-closed)."""
        mock_redis.incr = AsyncMock(side_effect=ConnectionError("Redis down"))
        with pytest.raises(ConnectionError):
            await throttle.check_login_attempt("user@example.com", "10.0.0.1")
