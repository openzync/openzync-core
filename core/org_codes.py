"""Organization join codes — generation and normalization.

An org code is the join token a new member presents at
``POST /v1/auth/join`` to join an existing organization.  Codes are
8 characters from a confusion-free alphabet (no I/O/0/1), stored
plaintext by explicit product decision.
"""

from __future__ import annotations

import secrets

ORG_CODE_ALPHABET: str = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
"""Confusion-free alphabet — excludes I, O, 0, 1."""

ORG_CODE_LENGTH: int = 8
"""Length of generated org codes (in characters)."""


def generate_org_code() -> str:
    """Generate a new random org code.

    Uses :func:`secrets.choice` — cryptographically secure, so codes are
    unguessable (31-char alphabet, 8 positions ≈ 39.6 bits of entropy).

    Returns:
        An 8-character org code, e.g. ``"K7M2Q9X4"``.
    """
    return "".join(secrets.choice(ORG_CODE_ALPHABET) for _ in range(ORG_CODE_LENGTH))


def normalize_org_code(code: str) -> str:
    """Normalize a user-supplied org code for lookup.

    Strips surrounding whitespace and uppercases — codes are case-insensitive.

    Args:
        code: The raw code as submitted by the client.

    Returns:
        The normalized code, e.g. ``" k7m2q9x4 "`` -> ``"K7M2Q9X4"``.
    """
    return code.strip().upper()
