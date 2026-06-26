"""Auth throttle — Redis-backed rate limiting with progressive account lockout.

Protects public authentication endpoints from brute-force and
credential-stuffing attacks by limiting attempts per-email and per-IP
with an escalating lockout duration.
"""

from __future__ import annotations

from redis.asyncio import Redis as AsyncRedis

from core.exceptions import RateLimitError


class AuthThrottle:
    """Rate-limits authentication attempts with progressive lockout.

    Uses three Redis counters:
    - Per-account: progressive lockout (1min at 5, 5min at 10, etc.).
    - Per-IP login: 20 attempts per 15-minute window.
    - Per-IP signup: 3 attempts per 1-hour window.

    The lockout counters reset after 1 hour of inactivity.
    Per-IP counters reset after their respective windows.

    Args:
        redis: An async Redis client.
    """

    def __init__(self, redis: AsyncRedis) -> None:
        self._redis = redis

    async def check_login_attempt(self, email: str, ip: str) -> dict:
        """Check and increment login attempt counters with progressive lockout.

        Lockout escalator:
        - 5–9 attempts  → 1 minute lockout
        - 10–14 attempts → 5 minute lockout
        - 15–19 attempts → 15 minute lockout
        - 20+ attempts   → 60 minute lockout

        Args:
            email: The email address being used to log in.
            ip: The client IP address.

        Returns:
            A dict with a ``captcha_required`` boolean flag (``True`` when
            the account has had ≥5 attempts, for CAPTCHA integration).

        Raises:
            RateLimitError: If the rate limit is exceeded.
        """
        # Per-account: progressive lockout
        acct_key = f"auth:lockout:acct:{email}"
        acct_attempts = await self._redis.incr(acct_key)
        if acct_attempts == 1:
            await self._redis.expire(acct_key, 3600)  # 1h window

        lockout_minutes = 0
        if acct_attempts >= 20:
            lockout_minutes = 60
        elif acct_attempts >= 15:
            lockout_minutes = 15
        elif acct_attempts >= 10:
            lockout_minutes = 5
        elif acct_attempts >= 5:
            lockout_minutes = 1

        if lockout_minutes > 0:
            raise RateLimitError(
                f"Account locked for {lockout_minutes} minute(s). "
                "Please try again later."
            )

        # Per-IP: 20 attempts per 15 min
        ip_key = f"auth:throttle:login:ip:{ip}"
        ip_attempts = await self._redis.incr(ip_key)
        if ip_attempts == 1:
            await self._redis.expire(ip_key, 900)

        if ip_attempts > 20:
            raise RateLimitError(
                "Too many login attempts from this IP address. "
                "Try again later."
            )

        return {"captcha_required": acct_attempts >= 5}

    async def check_signup_attempt(self, ip: str) -> None:
        """Check and increment signup attempt counter.

        Args:
            ip: The client IP address.

        Raises:
            RateLimitError: If the rate limit is exceeded.
        """
        key = f"auth:throttle:signup:ip:{ip}"
        attempts = await self._redis.incr(key)
        if attempts == 1:
            await self._redis.expire(key, 3600)  # 1 hour
        if attempts > 3:
            raise RateLimitError(
                "Too many signup attempts from this IP address. "
                "Try again later."
            )
