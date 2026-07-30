"""Unit tests for password utilities (utils/password.py).

Covers:
  - ``hash_password`` — bcrypt hashing with salt.
  - ``verify_password`` — correct / incorrect / edge-case verification.
"""

from __future__ import annotations

import pytest


@pytest.mark.unit
class TestHashPassword:
    """hash_password() — bcrypt hashing."""

    VALID_PASSWORD = "ValidPass123!"

    def test_hash_starts_with_bcrypt_prefix(self) -> None:
        """Hashed password starts with $2b$ (bcrypt)."""
        from utils.password import hash_password

        hashed = hash_password(self.VALID_PASSWORD)
        assert hashed.startswith("$2b$")

    def test_hash_is_60_characters(self) -> None:
        """bcrypt hash is exactly 60 characters."""
        from utils.password import hash_password

        hashed = hash_password(self.VALID_PASSWORD)
        assert len(hashed) == 60

    def test_different_passwords_different_hashes(self) -> None:
        """Different passwords produce different hashes."""
        from utils.password import hash_password

        h1 = hash_password("Password1!")
        h2 = hash_password("Password2@")
        assert h1 != h2

    def test_same_password_different_hashes(self) -> None:
        """Same password produces different hashes (random salt)."""
        from utils.password import hash_password

        h1 = hash_password(self.VALID_PASSWORD)
        h2 = hash_password(self.VALID_PASSWORD)
        assert h1 != h2  # different salts → different hashes

    def test_empty_password_raises_value_error(self) -> None:
        """Empty string raises ValueError."""
        from utils.password import hash_password

        with pytest.raises(ValueError, match="Password must not be empty"):
            hash_password("")

    def test_unicode_password(self) -> None:
        """Unicode password is accepted."""
        from utils.password import hash_password

        hashed = hash_password("Passwörd123!ñ")
        assert hashed.startswith("$2b$")
        assert len(hashed) == 60

    def test_long_password(self) -> None:
        """Very long password (72 bytes) is accepted."""
        from utils.password import hash_password

        long_pw = "A" * 60 + "1!"
        hashed = hash_password(long_pw)
        assert hashed.startswith("$2b$")


@pytest.mark.unit
class TestVerifyPassword:
    """verify_password() — correctness and edge cases."""

    VALID_PASSWORD = "ValidPass123!"
    WRONG_PASSWORD = "WrongPass!"

    def test_correct_password_verifies(self) -> None:
        """Correct password returns True."""
        from utils.password import hash_password, verify_password

        hashed = hash_password(self.VALID_PASSWORD)
        assert verify_password(self.VALID_PASSWORD, hashed) is True

    def test_incorrect_password_fails(self) -> None:
        """Wrong password returns False."""
        from utils.password import hash_password, verify_password

        hashed = hash_password(self.VALID_PASSWORD)
        assert verify_password(self.WRONG_PASSWORD, hashed) is False

    def test_wrong_hash_returns_false(self) -> None:
        """Invalid hash string returns False (does not crash)."""
        from utils.password import verify_password

        assert verify_password(self.VALID_PASSWORD, "$2b$12$invalidhash") is False

    def test_empty_password_returns_false(self) -> None:
        """Empty password against a valid hash returns False."""
        from utils.password import hash_password, verify_password

        hashed = hash_password(self.VALID_PASSWORD)
        assert verify_password("", hashed) is False

    def test_empty_hash_returns_false(self) -> None:
        """Valid password against empty hash returns False."""
        from utils.password import verify_password

        assert verify_password(self.VALID_PASSWORD, "") is False

    def test_unicode_password_roundtrip(self) -> None:
        """Unicode password verifies correctly."""
        from utils.password import hash_password, verify_password

        hashed = hash_password("Passwörd123!ñ")
        assert verify_password("Passwörd123!ñ", hashed) is True

    def test_invalid_hash_format_returns_false(self) -> None:
        """Invalid hash format returns False (bcrypt raises ValueError, caught)."""
        from utils.password import verify_password

        assert verify_password("ValidPass123!", "not-a-bcrypt-hash") is False
