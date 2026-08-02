"""Unit tests for base64 cursor encode/decode for pagination."""

from __future__ import annotations

import pytest


@pytest.mark.unit
class TestEncodeCursor:
    """encode_cursor encodes strings to URL-safe base64."""

    def test_encodes_simple_string(self) -> None:
        """Simple string is encoded to URL-safe base64 without padding."""
        from core.cursor import encode_cursor

        result = encode_cursor("hello")
        # "hello" in b64 = "aGVsbG8=", stripped of "="
        assert result == "aGVsbG8"
        assert "=" not in result

    def test_encodes_uuid_string(self) -> None:
        """UUID string is encoded correctly."""
        from core.cursor import encode_cursor

        uuid_str = "123e4567-e89b-12d3-a456-426614174000"
        result = encode_cursor(uuid_str)
        assert result != uuid_str
        assert isinstance(result, str)

    def test_encodes_empty_string(self) -> None:
        """Empty string produces empty result."""
        from core.cursor import encode_cursor

        result = encode_cursor("")
        assert result == ""

    def test_encodes_complex_string(self) -> None:
        """String with special chars survives round-trip."""
        from core.cursor import encode_cursor

        original = "user:abc123|ts:2024-01-15T10:30:00"
        result = encode_cursor(original)
        # Should have no padding
        assert "=" not in result
        assert result != original


@pytest.mark.unit
class TestDecodeCursor:
    """decode_cursor decodes base64 strings back to original."""

    def test_decodes_valid_base64(self) -> None:
        """Valid base64 is decoded back to original string."""
        from core.cursor import decode_cursor

        result = decode_cursor("aGVsbG8")
        assert result == "hello"

    def test_decodes_padded_base64(self) -> None:
        """Base64 with padding is decoded correctly."""
        from core.cursor import decode_cursor

        result = decode_cursor("aGVsbG8=")
        assert result == "hello"

    def test_decodes_double_padded_base64(self) -> None:
        """Base64 with double padding is decoded correctly."""
        from core.cursor import decode_cursor

        result = decode_cursor("dGVzdA==")
        assert result == "test"

    def test_round_trip_uuid(self) -> None:
        """Encode then decode returns the original string."""
        from core.cursor import decode_cursor, encode_cursor

        original = "123e4567-e89b-12d3-a456-426614174000"
        encoded = encode_cursor(original)
        decoded = decode_cursor(encoded)
        assert decoded == original

    def test_round_trip_complex(self) -> None:
        """Complex cursor strings survive round-trip."""
        from core.cursor import decode_cursor, encode_cursor

        original = "user:abc123|ts:2024-01-15T10:30:00|sort:name"
        encoded = encode_cursor(original)
        decoded = decode_cursor(encoded)
        assert decoded == original

    def test_round_trip_empty(self) -> None:
        """Empty string survives round-trip."""
        from core.cursor import decode_cursor, encode_cursor

        encoded = encode_cursor("")
        decoded = decode_cursor(encoded)
        assert decoded == ""


@pytest.mark.unit
class TestDecodeCursorErrors:
    """decode_cursor error handling."""

    def test_invalid_base64_raises_value_error(self) -> None:
        """Malformed base64 raises ValueError."""
        from core.cursor import decode_cursor

        with pytest.raises(ValueError, match="Invalid cursor"):
            decode_cursor("!!!not-base64!!!")

    def test_invalid_unicode_raises_value_error(self) -> None:
        """Base64 that decodes to invalid UTF-8 raises ValueError."""
        from core.cursor import decode_cursor

        with pytest.raises(ValueError, match="Invalid cursor"):
            decode_cursor("////")  # decodes to non-UTF-8 bytes
