"""Auth throttle — Redis-backed rate limiting for login/signup endpoints.

Protects public authentication endpoints from brute-force and
credential-stuffing attacks by limiting attempts per-email and per-IP.
"""

from __future__ import annotations

from redis.asyncio import Redis as AsyncRedis

from core.exceptions import RateLimitError


class AuthThrottle:
    """Rate-limits authentication attempts per-email and per-IP.

    Uses two Redis counters:
    - Per-email: 5 attempts per 15-minute window.
    - Per-IP:    20 attempts per 15-minute window for login,
                  3 attempts per 1-hour window for signup.

    Args:
        redis: An async Redis client.
    """

    def __init__(self, redis: AsyncRedis) -> None:
        self._redis = redis

    async def check_login_attempt(self, email: str, ip: str) -> None:
        """Check and increment login attempt counters.

        Args:
            email: The email address being used to log in.
            ip: The client IP address.

        Raises:
            RateLimitError: If the rate limit is exceeded.
        """
        # Per-email: 5 attempts per 15 min
        email_key = f"auth:throttle:login:email:{email}"
        email_attempts = await self._redis.incr(email_key)
        if email_attempts == 1:
            await self._redis.expire(email_key, 900)

        # Per-IP: 20 attempts per 15 min
        ip_key = f"auth:throttle:login:ip:{ip}"
        ip_attempts = await self._redis.incr(ip_key)
        if ip_attempts == 1:
            await self._redis.expire(ip_key, 900)

        if email_attempts > 5:
            raise RateLimitError(
                "Too many login attempts for this account. "
                "Try again later."
            )
        if ip_attempts > 20:
            raise RateLimitError(
                "Too many login attempts from this IP address. "
                "Try again later."
            )

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
