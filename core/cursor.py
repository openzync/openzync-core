"""Cursor encoding/decoding helpers for cursor-based pagination.

Each repository defines its own cursor payload format; this module
provides only the base64 encode/decode primitives plus a versioned
envelope (``v1:<payload>``) so stale cursors from older releases fail
loudly instead of silently returning wrong pages.

BREAKING: cursors issued before versioning carry no version prefix and
are rejected with :class:`CursorExpiredError` — clients must restart
pagination from the first page.
"""

from __future__ import annotations

import base64

from core.exceptions import CursorExpiredError

CURSOR_VERSION: str = "v1"
"""Current cursor envelope version. Bump when a payload format changes."""


def encode_cursor(value: str) -> str:
    """Encode a cursor value as a URL-safe base64 string without padding.

    Args:
        value: The raw cursor string to encode.

    Returns:
        A URL-safe base64 encoded string (no padding).
    """
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> str:
    """Decode a URL-safe base64 cursor string.

    Args:
        cursor: The base64-encoded cursor string (with or without padding).

    Returns:
        The decoded raw cursor string.

    Raises:
        ValueError: If the cursor is malformed.
    """
    try:
        padding = 4 - len(cursor) % 4
        if padding != 4:
            cursor += "=" * padding
        return base64.urlsafe_b64decode(cursor.encode()).decode()
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid cursor: {e}") from e


def encode_versioned_cursor(payload: str) -> str:
    """Encode a cursor payload inside the versioned envelope.

    Args:
        payload: The raw cursor payload (e.g. ``"42|<episode_hex>"``).

    Returns:
        A URL-safe base64 string of ``"v1:<payload>"`` (no padding).
    """
    return encode_cursor(f"{CURSOR_VERSION}:{payload}")


def decode_versioned_cursor(cursor: str) -> str:
    """Decode a versioned cursor back to its raw payload.

    Args:
        cursor: The opaque cursor string from a previous response.

    Returns:
        The raw payload without the version prefix.

    Raises:
        CursorExpiredError: If the cursor is malformed or carries an
            unsupported version (including pre-versioning cursors, which
            have no prefix). Maps to HTTP 400 ``cursor_expired``.
    """
    try:
        raw = decode_cursor(cursor)
    except ValueError as e:
        raise CursorExpiredError(f"Invalid cursor: {e}") from e
    version, sep, payload = raw.partition(":")
    if not sep or version != CURSOR_VERSION or not payload:
        raise CursorExpiredError(
            f"Invalid cursor: unsupported cursor version {version!r} "
            f"(expected {CURSOR_VERSION!r}) — restart pagination"
        )
    return payload
