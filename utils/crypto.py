"""Cryptographic utilities for API key generation, hashing, and JWT management.

All key material is generated from ``secrets`` (CSPRNG).  API keys use SHA-256
with a random 16-byte salt — never bcrypt (too slow for per-request auth).
JWT tokens use HS256 with the application secret key.

Usage:
    from utils.crypto import generate_api_key, hash_api_key, verify_api_key

    raw_key = generate_api_key()
    key_hash, salt = hash_api_key(raw_key)
    assert verify_api_key(raw_key, key_hash, salt)
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from core.exceptions import AuthenticationError

# ═══════════════════════════════════════════════════════════════════════════════
# Base62 encoding — no external dependency required
# ═══════════════════════════════════════════════════════════════════════════════

BASE62_ALPHABET: str = (
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)
"""Base62 character set: 0-9 (10), A-Z (26), a-z (26) = 62 chars."""


def base62_encode(num: int) -> str:
    """Encode a non-negative integer as a base62 string.

    Args:
        num: Arbitrary non-negative integer.

    Returns:
        Base62-encoded string (least significant digit last).

    Raises:
        ValueError: If ``num`` is negative.
    """
    if num < 0:
        raise ValueError(f"Cannot encode negative integer: {num}")
    if num == 0:
        return BASE62_ALPHABET[0]

    result: list[str] = []
    while num > 0:
        num, rem = divmod(num, 62)
        result.append(BASE62_ALPHABET[rem])
    return "".join(reversed(result))


# ═══════════════════════════════════════════════════════════════════════════════
# API key generation & hashing
# ═══════════════════════════════════════════════════════════════════════════════


def generate_api_key(prefix: str = "mg_live_") -> str:
    """Generate a cryptographically random API key.

    The key consists of 48 CSPRNG bytes encoded as base62 (≈ 67 chars) with
    a human-readable prefix.  Total length ≈ 73 characters.

    Args:
        prefix: Optional key prefix for identifying the environment or owner.
            Defaults to ``"mg_live_"`` (production OpenZep).  Use
            ``"mg_test_"`` for test/development keys.

    Returns:
        Full API key string, e.g. ``"mg_live_3Ab9...kQ7"``.

    Example:
        >>> key = generate_api_key("mg_test_")
        >>> len(key) > 70
        True
        >>> key.startswith("mg_test_")
        True
    """
    raw = secrets.token_bytes(48)
    encoded = base62_encode(int.from_bytes(raw, "big"))
    return f"{prefix}{encoded}"


def hash_api_key(raw_key: str) -> tuple[str, str]:
    """Hash an API key with a random 16-byte salt.

    Uses ``SHA-256(salt || raw_key)`` — the hash is stored in the database
    alongside the salt for verification.  The salt is regenerated on every call.

    Args:
        raw_key: The full API key string as returned by :func:`generate_api_key`.

    Returns:
        Tuple of ``(hex_hash, hex_salt)``.
        - ``hex_hash``: 64-character hex string (SHA-256 output).
        - ``hex_salt``: 32-character hex string (16 random bytes).
    """
    salt = secrets.token_hex(16)  # 16 bytes → 32 hex chars
    hash_value = hashlib.sha256(f"{salt}{raw_key}".encode()).hexdigest()
    return hash_value, salt


def verify_api_key(raw_key: str, stored_hash: str, salt: str) -> bool:
    """Verify an API key against its stored salted hash.

    Computes ``SHA-256(salt || raw_key)`` and compares it to ``stored_hash``.
    Constant-time comparison is **not** used here because the hash is not
    a password — timing attacks on API keys are impractical at network scale.

    Args:
        raw_key: The full API key string provided by the client.
        stored_hash: The 64-character hex hash stored in the database.
        salt: The 32-character hex salt stored alongside the hash.

    Returns:
        ``True`` if the key matches, ``False`` otherwise.
    """
    computed = hashlib.sha256(f"{salt}{raw_key}".encode()).hexdigest()
    return computed == stored_hash


def compute_lookup_hash(raw_key: str) -> str:
    """Compute a deterministic, **unsalted** hash for fast cache/DB lookup.

    This is **not** a security hash — it is used purely for indexing and
    caching.  The salted :func:`hash_api_key` is used for verification.

    Args:
        raw_key: The full API key string.

    Returns:
        64-character hex string (SHA-256 of the raw key, no salt).
    """
    return hashlib.sha256(raw_key.encode()).hexdigest()


# ═══════════════════════════════════════════════════════════════════════════════
# JWT helpers
# ═══════════════════════════════════════════════════════════════════════════════


def create_jwt_token(
    data: dict[str, Any],
    secret: str,
    expires_delta: timedelta,
) -> str:
    """Create a signed JWT token (HS256).

    Args:
        data: Payload claims to encode (e.g. ``{"sub": user_id, "org_id": ...}``).
        secret: HMAC secret key (must be at least 32 chars in production).
        expires_delta: Relative duration until the token expires.

    Returns:
        Encoded JWT string (three base64url segments separated by dots).

    Raises:
        jwt.PyJWTError: If encoding fails.
    """
    import jwt

    to_encode: dict[str, Any] = data.copy()
    now = datetime.now(timezone.utc)
    to_encode.update(
        {
            "exp": now + expires_delta,
            "iat": now,
        }
    )
    return jwt.encode(to_encode, secret, algorithm="HS256")


def verify_jwt_token(token: str, secret: str) -> dict[str, Any]:
    """Verify and decode a JWT token.

    Args:
        token: Encoded JWT string.
        secret: HMAC secret key used for signing.

    Returns:
        Decoded payload as a dictionary.

    Raises:
        AuthenticationError: If the token is expired or invalid.
    """
    import jwt

    try:
        return jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise AuthenticationError("Token expired")
    except jwt.InvalidTokenError:
        raise AuthenticationError("Invalid token")
