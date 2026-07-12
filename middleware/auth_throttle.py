"""Auth throttle — Redis-backed rate limiting for login/signup endpoints.

Protects public authentication endpoints from brute-force and
credential-stuffing attacks by limiting attempts per-email and per-IP.
"""

from __future__ import annotations

from redis.asyncio import Redis as AsyncRedis

from core.exceptions import RateLimitError


class AuthThrottle:
    """Rate-limits authentication attempts per-email and per-IP.

    Uses Redis counters for login (per-email and per-IP) and signup
    (per-IP).  Limits are configurable at construction time; defaults
    match the original hardcoded values for backward compatibility.

    Args:
        redis: An async Redis client.
        login_max_per_ip: Max failed login attempts per IP before
            throttling (default 20).
        login_window_sec: Login throttle window in seconds (default 900
            = 15 minutes).
        login_max_per_email: Max failed login attempts per email before
            throttling (default 5).
        signup_max_per_ip: Max signup attempts per IP before throttling
            (default 3).
        signup_window_sec: Signup throttle window in seconds (default
            3600 = 1 hour).
    """

    def __init__(
        self,
        redis: AsyncRedis,
        login_max_per_ip: int = 20,
        login_window_sec: int = 900,
        login_max_per_email: int = 5,
        signup_max_per_ip: int = 3,
        signup_window_sec: int = 3600,
    ) -> None:
        self._redis = redis
        self._login_max_per_ip = login_max_per_ip
        self._login_window_sec = login_window_sec
        self._login_max_per_email = login_max_per_email
        self._signup_max_per_ip = signup_max_per_ip
        self._signup_window_sec = signup_window_sec

    async def check_login_attempt(self, email: str, ip: str) -> None:
        """Check and increment login attempt counters.

        Args:
            email: The email address being used to log in.
            ip: The client IP address.

        Raises:
            RateLimitError: If the rate limit is exceeded.
        """
        email_key = f"auth:throttle:login:email:{email}"
        email_attempts = await self._redis.incr(email_key)
        if email_attempts == 1:
            await self._redis.expire(email_key, self._login_window_sec)

        ip_key = f"auth:throttle:login:ip:{ip}"
        ip_attempts = await self._redis.incr(ip_key)
        if ip_attempts == 1:
            await self._redis.expire(ip_key, self._login_window_sec)

        if email_attempts > self._login_max_per_email:
            raise RateLimitError(
                "Too many login attempts for this account. "
                "Try again later."
            )
        if ip_attempts > self._login_max_per_ip:
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
            await self._redis.expire(key, self._signup_window_sec)
        if attempts > self._signup_max_per_ip:
            raise RateLimitError(
                "Too many signup attempts from this IP address. "
                "Try again later."
            )

    async def check_verify_attempt(self, email: str, ip: str) -> None:
        """Check and increment email-verification attempt counter.

        Protects the ``/v1/auth/verify-email`` endpoint from brute-force
        OTP guessing.

        Args:
            email: The email being verified.
            ip: The client IP address.

        Raises:
            RateLimitError: If the rate limit is exceeded.
        """
        email_key = f"auth:throttle:verify:email:{email}"
        email_attempts = await self._redis.incr(email_key)
        if email_attempts == 1:
            await self._redis.expire(email_key, 900)  # 15 min window

        ip_key = f"auth:throttle:verify:ip:{ip}"
        ip_attempts = await self._redis.incr(ip_key)
        if ip_attempts == 1:
            await self._redis.expire(ip_key, 900)  # 15 min window

        if email_attempts > 10:
            raise RateLimitError(
                "Too many verification attempts for this email. "
                "Please request a new code."
            )
        if ip_attempts > 20:
            raise RateLimitError(
                "Too many verification attempts from this IP address. "
                "Try again later."
            )
