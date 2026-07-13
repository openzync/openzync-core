"""OTP service — one-time passcode generation, storage, and verification.

All OTPs are stored in Redis as SHA-256 hashes with automatic TTL expiry.
The service enforces per-email send limits, resend cooldowns, and attempt caps.

Key design decisions:
    - OTPs are **never stored in plaintext** — only SHA-256 hashes go to Redis.
    - OTPs are **purpose-scoped** — a code generated for ``signup`` cannot be
      used for ``password_reset``.
    - Verification uses **constant-time comparison** via ``hmac.compare_digest``.
    - Rate limits are enforced inside ``generate_and_send`` using Redis counters.

Redis key schema::
    otp:v1:{purpose}:{email}:hash       → sha256(otp)        TTL 600s
    otp:v1:{purpose}:{email}:attempts   → counter (int)      TTL 600s
    otp:v1:{purpose}:{email}:cooldown   → "1"                TTL 60s
    email:send_count:{email}:{yyyymmdd} → counter (int)      TTL 3600s
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from core.exceptions import RateLimitError, ValidationError

if TYPE_CHECKING:
    import redis.asyncio as aioredis

    from services.email_service import EmailService

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

OtpPurpose = Literal["signup", "password_reset", "passwordless_login", "mfa"]
"""Valid OTP purpose identifiers — scopes prevent cross-use."""

_OTP_TTL_SEC = 600  # 10 minutes
"""Time-to-live for a generated OTP hash in Redis (seconds)."""

_ATTEMPT_TTL_SEC = 600  # 10 minutes
"""Time-to-live for the attempt counter (same window as the OTP)."""

_COOLDOWN_SEC = 60  # 1 minute
"""Minimum interval between OTP resend requests for the same email."""

_MAX_SENDS_PER_HOUR = 5
"""Maximum number of OTP send requests per email per rolling hour."""

_MAX_ATTEMPTS = 5
"""Maximum failed verification attempts before the OTP is invalidated."""

_OTP_LENGTH = 6
"""Number of digits in the generated OTP."""

_REDIS_KEY_PREFIX = "otp:v1"
"""Prefix for all OTP-related Redis keys."""


# ═══════════════════════════════════════════════════════════════════════════════
# Redis key helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _otp_hash_key(email: str, purpose: OtpPurpose) -> str:
    """Redis key storing the SHA-256 hash of the OTP.

    Args:
        email: User's email address (lowercased).
        purpose: OTP purpose scope.

    Returns:
        Redis key string.
    """
    return f"{_REDIS_KEY_PREFIX}:{purpose}:{email.lower()}:hash"


def _attempts_key(email: str, purpose: OtpPurpose) -> str:
    """Redis key storing the attempt counter for this OTP.

    Args:
        email: User's email address (lowercased).
        purpose: OTP purpose scope.

    Returns:
        Redis key string.
    """
    return f"{_REDIS_KEY_PREFIX}:{purpose}:{email.lower()}:attempts"


def _cooldown_key(email: str, purpose: OtpPurpose) -> str:
    """Redis key tracking the resend cooldown for this OTP.

    Note: the argument is typed as ``OtpPurpose`` but we pass raw strings
    from ``_check_rate_limits``.  That is intentional — we iterate over all
    known purpose strings at verification time.

    Args:
        email: User's email address (lowercased).
        purpose: OTP purpose scope.

    Returns:
        Redis key string.
    """
    return f"{_REDIS_KEY_PREFIX}:{purpose}:{email.lower()}:cooldown"


def _send_count_key(email: str) -> str:
    """Redis key for the hourly send-count counter.

    The hour bucket is ``YYYYMMDDHH`` so the key auto-expires after 1 hour.

    Args:
        email: User's email address (lowercased).

    Returns:
        Redis key string.
    """
    hour_bucket = datetime.now(UTC).strftime("%Y%m%d%H")
    return f"email:send_count:{email.lower()}:{hour_bucket}"


# ═══════════════════════════════════════════════════════════════════════════════
# Service
# ═══════════════════════════════════════════════════════════════════════════════


class OtpService:
    """One-time passcode lifecycle — generate, send, verify, invalidate.

    All state lives in Redis (auto-expiring keys).  No DB persistence.

    Args:
        redis: Async Redis client from the application lifespan.
        email_service: ``EmailService`` instance used to deliver OTP emails.
    """

    __slots__ = ("_redis", "_email_service")

    def __init__(self, redis: aioredis.Redis, email_service: EmailService) -> None:  # noqa: F821
        self._redis = redis
        self._email_service = email_service

    # ══════════════════════════════════════════════════════════════════════
    # Public API
    # ══════════════════════════════════════════════════════════════════════

    async def generate_and_send(
        self,
        email: str,
        purpose: OtpPurpose,
    ) -> None:
        """Generate a secure OTP, store its hash in Redis, and send via email.

        Enforces per-email send limits and resend cooldown.  The plaintext
        OTP is **never persisted** — only its SHA-256 hash is stored.

        Args:
            email: Recipient email address.
            purpose: Purpose scope (``signup``, ``password_reset``, etc.).

        Raises:
            RateLimitError: If the email has exceeded the send limit or
                the resend cooldown is active.
            ExternalServiceError: If email delivery fails.
        """
        email_key = email.lower()

        # ── Abuse controls ───────────────────────────────────────────────
        await self._check_rate_limits(email_key)

        # ── Generate secure OTP ──────────────────────────────────────────
        otp = self._generate_otp()
        otp_hash = self._hash_otp(otp)

        # ── Store hash in Redis (auto-expiring) ──────────────────────────
        pipe = self._redis.pipeline()
        await pipe.setex(_otp_hash_key(email_key, purpose), _OTP_TTL_SEC, otp_hash)
        await pipe.setex(_attempts_key(email_key, purpose), _ATTEMPT_TTL_SEC, 0)
        await pipe.setex(_cooldown_key(email_key, purpose), _COOLDOWN_SEC, "1")
        await pipe.incr(_send_count_key(email_key))
        await pipe.expire(_send_count_key(email_key), 3600)
        await pipe.execute()

        # ── Send via email service ───────────────────────────────────────
        await self._send_otp_email(email_key, purpose, otp)

    async def verify(
        self,
        email: str,
        purpose: OtpPurpose,
        code: str,
    ) -> bool:
        """Verify an OTP code against the stored hash.

        On success, the OTP hash is deleted (single-use).  On failure, the
        attempt counter is incremented and the OTP is invalidated after
        :const:`_MAX_ATTEMPTS` failed tries.

        Args:
            email: Email address the OTP was sent to.
            purpose: Purpose scope the OTP was generated for.
            code: The plaintext OTP code entered by the user.

        Returns:
            ``True`` if the code matches and is within the validity window,
            ``False`` otherwise.

        Raises:
            ValidationError: If the attempt limit has been exceeded.
        """
        email_key = email.lower()
        hash_key = _otp_hash_key(email_key, purpose)
        attempts_key = _attempts_key(email_key, purpose)

        stored_hash: str | None = await self._redis.get(hash_key)
        if stored_hash is None:
            return False

        attempts: int = await self._redis.incr(attempts_key)
        if attempts > _MAX_ATTEMPTS:
            await self._redis.delete(
                hash_key, attempts_key, _cooldown_key(email_key, purpose),
            )
            raise ValidationError(
                "Too many failed attempts.  Please request a new code.",
            )

        input_hash = self._hash_otp(code.strip())
        if hmac.compare_digest(input_hash, stored_hash):
            await self._redis.delete(
                hash_key, attempts_key, _cooldown_key(email_key, purpose),
            )
            logger.info(
                "otp.verified",
                extra={"purpose": purpose, "email": _mask_email(email_key)},
            )
            return True

        logger.info(
            "otp.verify_failed",
            extra={
                "purpose": purpose,
                "email": _mask_email(email_key),
                "attempts": attempts,
            },
        )
        return False

    async def invalidate(self, email: str, purpose: OtpPurpose) -> None:
        """Invalidate all OTP-related keys for a given email and purpose.

        Call this after a successful password change or login to ensure
        no residual OTP can be used.

        Args:
            email: Email address to invalidate.
            purpose: Purpose scope to invalidate.
        """
        email_key = email.lower()
        await self._redis.delete(
            _otp_hash_key(email_key, purpose),
            _attempts_key(email_key, purpose),
            _cooldown_key(email_key, purpose),
        )

    # ══════════════════════════════════════════════════════════════════════
    # Internal helpers
    # ══════════════════════════════════════════════════════════════════════

    async def _send_otp_email(
        self,
        email_key: str,
        purpose: OtpPurpose,
        otp: str,
    ) -> None:
        """Render and send the OTP email.

        Args:
            email_key: Lowercased recipient email.
            purpose: OTP purpose (for logging).
            otp: The plaintext OTP to include in the email.
        """
        from services.email_service import (  # noqa: PLC0415
            render_email_template,
            render_text_template,
        )

        context: dict[str, object] = {
            "code": otp,
            "expiry_minutes": _OTP_TTL_SEC // 60,
        }
        html_body = await render_email_template("otp", context)
        text_body = await render_text_template("otp", context)

        await self._email_service.send_email(
            to=email_key,
            subject="Your OpenZync verification code",
            html_body=html_body,
            text_body=text_body,
        )

        logger.info(
            "otp.sent",
            extra={"purpose": purpose, "email": _mask_email(email_key)},
        )

    async def _check_rate_limits(self, email_key: str) -> None:
        """Enforce per-email send limits and resend cooldown.

        Args:
            email_key: Lowercased email address.

        Raises:
            RateLimitError: If send limit exceeded or cooldown active.
        """
        for purpose_str in ("signup", "password_reset", "passwordless_login", "mfa"):
            if await self._redis.exists(  # type: ignore[arg-type]
                _cooldown_key(email_key, purpose_str),
            ):
                raise RateLimitError(
                    "Please wait before requesting another code. "
                    "You can request a new code in about a minute.",
                )

        send_count = await self._redis.get(_send_count_key(email_key))
        if send_count is not None and int(send_count) >= _MAX_SENDS_PER_HOUR:
            raise RateLimitError(
                "You have reached the maximum number of OTP requests "
                "for this email address.  Please try again later.",
            )

    @staticmethod
    def _generate_otp() -> str:
        """Generate a cryptographically secure numeric OTP.

        Returns:
            A zero-padded ``_OTP_LENGTH``-digit string.
        """
        return f"{secrets.randbelow(10 ** _OTP_LENGTH):0{_OTP_LENGTH}d}"

    @staticmethod
    def _hash_otp(code: str) -> str:
        """Return the SHA-256 hex digest of an OTP string.

        Args:
            code: The plaintext OTP.

        Returns:
            Hex-encoded SHA-256 digest.
        """
        return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _mask_email(email: str) -> str:
    """Mask email for logging — shows first and last char of local part.

    Args:
        email: The full email address.

    Returns:
        Masked string (e.g. ``u**r@example.com``).
    """
    local, _, domain = email.partition("@")
    if len(local) <= 1:
        return f"{local[0]}**@{domain}" if local else email
    return f"{local[0]}**{local[-1]}@{domain}"
