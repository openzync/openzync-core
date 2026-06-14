"""Cursor encoding/decoding helpers for cursor-based pagination.

Each repository defines its own cursor payload format; this module
provides only the base64 encode/decode primitives.
"""

from __future__ import annotations

import base64


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
