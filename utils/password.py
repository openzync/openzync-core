"""Password hashing and verification using bcrypt.

All dashboard user passwords are hashed with bcrypt before storage.
The application never stores or logs plaintext passwords.

Usage:
    from utils.password import hash_password, verify_password

    hashed = hash_password("my-secure-password")
    assert verify_password("my-secure-password", hashed)
"""

from __future__ import annotations

import bcrypt


def hash_password(password: str) -> str:
    """Hash a password with a randomly generated salt (bcrypt).

    The resulting hash is a 60-character string that includes the salt
    and algorithm metadata (``$2b$12$...``).

    Args:
        password: The plaintext password to hash.

    Returns:
        bcrypt hash string (60 characters), safe for storage.

    Raises:
        ValueError: If the password is empty.
    """
    if not password:
        raise ValueError("Password must not be empty")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """Verify a plaintext password against a bcrypt hash.

    Args:
        password: The plaintext password to check.
        hashed: The stored bcrypt hash string.

    Returns:
        ``True`` if the password matches the hash, ``False`` otherwise.
    """
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"),
            hashed.encode("utf-8"),
        )
    except (ValueError, TypeError):
        return False
