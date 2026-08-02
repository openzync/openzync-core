"""Unit tests for cryptographic utilities (utils/crypto.py).

Covers:
  - ``base62_encode`` — integer encoding.
  - ``generate_api_key`` — key generation with prefix/length.
  - ``hash_api_key`` / ``verify_api_key`` — salted SHA-256 round-trip.
  - ``compute_lookup_hash`` — unsalted deterministic hash.
  - ``create_jwt_token`` / ``verify_jwt_token`` — HS256 JWT lifecycle.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from time import sleep
from uuid import uuid4

import pytest

from core.exceptions import AuthenticationError


# ═══════════════════════════════════════════════════════════════════════════════
# Base62
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestBase62Encode:
    """base62_encode() — integer-to-base62 conversion."""

    def test_zero_returns_first_alphabet_char(self) -> None:
        """0 encodes to '0'."""
        from utils.crypto import base62_encode

        assert base62_encode(0) == "0"

    def test_one_returns_second_alphabet_char(self) -> None:
        """1 encodes to '1'."""
        from utils.crypto import base62_encode

        assert base62_encode(1) == "1"

    def test_62_encodes_to_10(self) -> None:
        """62 encodes to '10' (1*62 + 0)."""
        from utils.crypto import base62_encode

        assert base62_encode(62) == "10"

    def test_large_integer_roundtrip(self) -> None:
        """Large integer can be encoded without error."""
        from utils.crypto import base62_encode

        large = 2**128
        result = base62_encode(large)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_negative_integer_raises(self) -> None:
        """Negative integers raise ValueError."""
        from utils.crypto import base62_encode

        with pytest.raises(ValueError, match="Cannot encode negative integer"):
            base62_encode(-1)

    def test_base62_alphabet_size(self) -> None:
        """BASE62_ALPHABET has exactly 62 characters."""
        from utils.crypto import BASE62_ALPHABET

        assert len(BASE62_ALPHABET) == 62
        assert BASE62_ALPHABET.isascii()


# ═══════════════════════════════════════════════════════════════════════════════
# API key generation
# ═══════════════════════════════════════════════════════════════════════════════


class TestGenerateApiKey:
    """generate_api_key() — format, prefix, entropy."""

    def test_default_prefix(self) -> None:
        """Default prefix is 'oz_live_'."""
        from utils.crypto import generate_api_key

        key = generate_api_key()
        assert key.startswith("oz_live_")

    def test_custom_prefix(self) -> None:
        """Custom prefix is used when provided."""
        from utils.crypto import generate_api_key

        key = generate_api_key(prefix="oz_test_")
        assert key.startswith("oz_test_")

    def test_minimum_length(self) -> None:
        """Generated key is at least 70 characters."""
        from utils.crypto import generate_api_key

        key = generate_api_key()
        assert len(key) >= 70

    def test_unique_keys(self) -> None:
        """Two generated keys are different (CSPRNG)."""
        from utils.crypto import generate_api_key

        keys = {generate_api_key() for _ in range(10)}
        assert len(keys) == 10

    def test_prefix_can_be_empty(self) -> None:
        """Empty prefix produces a key with just base62 payload."""
        from utils.crypto import generate_api_key

        key = generate_api_key(prefix="")
        # 48 random bytes → ceil(48 * log(256) / log(62)) ≈ 65 base62 chars
        assert len(key) >= 60
        assert not key.startswith("oz_")

    def test_contains_only_base62_chars_after_prefix(self) -> None:
        """The payload portion uses only base62 characters."""
        from utils.crypto import BASE62_ALPHABET, generate_api_key

        key = generate_api_key()
        payload = key.removeprefix("oz_live_")
        assert all(c in BASE62_ALPHABET for c in payload)


# ═══════════════════════════════════════════════════════════════════════════════
# API key hashing & verification
# ═══════════════════════════════════════════════════════════════════════════════


class TestHashAndVerifyApiKey:
    """hash_api_key() and verify_api_key()."""

    KEY = "oz_test_" + "a" * 64

    def test_hash_returns_hex_strings(self) -> None:
        """hash_api_key returns a (hex_hash, hex_salt) tuple of hex strings."""
        from utils.crypto import hash_api_key

        key_hash, salt = hash_api_key(self.KEY)
        assert isinstance(key_hash, str)
        assert isinstance(salt, str)
        # SHA-256 → 64 hex chars; 16 bytes salt → 32 hex chars
        assert len(key_hash) == 64
        assert len(salt) == 32
        int(key_hash, 16)  # does not raise
        int(salt, 16)  # does not raise

    def test_hash_is_deterministic_with_same_salt(self) -> None:
        """Same key + same salt produces same hash (verified via verify_api_key)."""
        from utils.crypto import hash_api_key, verify_api_key

        key_hash, salt = hash_api_key(self.KEY)
        # Verification uses the same algorithm — proof of determinism
        assert verify_api_key(self.KEY, key_hash, salt) is True

    def test_different_keys_different_hashes(self) -> None:
        """Different keys produce different hashes (with high probability)."""
        from utils.crypto import hash_api_key

        h1, _ = hash_api_key("oz_test_" + "a" * 64)
        h2, _ = hash_api_key("oz_test_" + "b" * 64)
        assert h1 != h2

    def test_verify_correct_key(self) -> None:
        """Correct key + stored hash + salt returns True."""
        from utils.crypto import hash_api_key, verify_api_key

        key = generate_api_key_for_test()
        key_hash, salt = hash_api_key(key)
        assert verify_api_key(key, key_hash, salt) is True

    def test_verify_wrong_key(self) -> None:
        """Wrong key returns False."""
        from utils.crypto import hash_api_key, verify_api_key

        key = generate_api_key_for_test()
        key_hash, salt = hash_api_key(key)
        assert verify_api_key("oz_test_xxxx", key_hash, salt) is False

    def test_verify_wrong_salt(self) -> None:
        """Correct key + wrong salt returns False."""
        from utils.crypto import hash_api_key, verify_api_key

        key = generate_api_key_for_test()
        key_hash, _ = hash_api_key(key)
        assert verify_api_key(key, key_hash, deadbeef_salt()) is False


class TestComputeLookupHash:
    """compute_lookup_hash() — unsalted, deterministic SHA-256."""

    def test_lookup_hash_is_hex_string(self) -> None:
        """Returns a 64-character hex string."""
        from utils.crypto import compute_lookup_hash

        h = compute_lookup_hash("oz_test_key")
        assert isinstance(h, str)
        assert len(h) == 64
        int(h, 16)  # does not raise

    def test_lookup_hash_is_deterministic(self) -> None:
        """Same input always produces the same hash."""
        from utils.crypto import compute_lookup_hash

        h1 = compute_lookup_hash("oz_test_key")
        h2 = compute_lookup_hash("oz_test_key")
        assert h1 == h2

    def test_lookup_hash_differs_for_different_keys(self) -> None:
        """Different inputs produce different hashes."""
        from utils.crypto import compute_lookup_hash

        assert compute_lookup_hash("key_a") != compute_lookup_hash("key_b")

    def test_lookup_hash_differs_from_salted_hash(self) -> None:
        """Lookup hash (unsalted) is different from salted hash of same key."""
        from utils.crypto import compute_lookup_hash, hash_api_key

        key = generate_api_key_for_test()
        salted_hash, _ = hash_api_key(key)
        lookup_hash = compute_lookup_hash(key)
        assert lookup_hash != salted_hash


# ═══════════════════════════════════════════════════════════════════════════════
# JWT helpers
# ═══════════════════════════════════════════════════════════════════════════════


class TestCreateJwtToken:
    """create_jwt_token() — token creation with claims."""

    # 32+ characters required by pyjwt to avoid InsecureKeyLengthWarning
    SECRET = "test-jwt-secret-key-32-chars-min!!!!!!"

    def test_creates_valid_jwt(self) -> None:
        """Created JWT can be decoded and has standard claims."""
        import jwt as pyjwt

        from utils.crypto import create_jwt_token

        user_id = str(uuid4())
        token = create_jwt_token(
            data={"sub": user_id, "role": "admin"},
            secret=self.SECRET,
            expires_delta=timedelta(hours=1),
        )
        decoded = pyjwt.decode(token, self.SECRET, algorithms=["HS256"])
        assert decoded["sub"] == user_id
        assert decoded["role"] == "admin"

    def test_contains_exp_and_iat(self) -> None:
        """Token includes exp (expiration) and iat (issued-at) claims."""
        import jwt as pyjwt

        from utils.crypto import create_jwt_token

        token = create_jwt_token(
            data={"sub": str(uuid4())},
            secret=self.SECRET,
            expires_delta=timedelta(hours=1),
        )
        decoded = pyjwt.decode(token, self.SECRET, algorithms=["HS256"])
        assert "exp" in decoded
        assert "iat" in decoded
        assert isinstance(decoded["exp"], int)
        assert isinstance(decoded["iat"], int)

    def test_iat_is_recent(self) -> None:
        """iat claim is within 5 seconds of now."""
        import jwt as pyjwt

        from utils.crypto import create_jwt_token

        before = int(datetime.now(timezone.utc).timestamp())
        token = create_jwt_token(
            data={"sub": str(uuid4())},
            secret=self.SECRET,
            expires_delta=timedelta(hours=1),
        )
        decoded = pyjwt.decode(token, self.SECRET, algorithms=["HS256"])
        assert decoded["iat"] >= before - 1
        assert decoded["iat"] <= before + 5

    def test_exp_is_in_future(self) -> None:
        """exp claim is in the future (not expired immediately)."""
        import jwt as pyjwt

        from utils.crypto import create_jwt_token

        now = int(datetime.now(timezone.utc).timestamp())
        token = create_jwt_token(
            data={"sub": str(uuid4())},
            secret=self.SECRET,
            expires_delta=timedelta(hours=1),
        )
        decoded = pyjwt.decode(token, self.SECRET, algorithms=["HS256"])
        assert decoded["exp"] > now

    def test_two_tokens_different(self) -> None:
        """Two tokens with different payloads are different."""
        from utils.crypto import create_jwt_token

        t1 = create_jwt_token({"sub": str(uuid4())}, self.SECRET, timedelta(hours=1))
        t2 = create_jwt_token({"sub": str(uuid4())}, self.SECRET, timedelta(hours=1))
        assert t1 != t2


class TestVerifyJwtToken:
    """verify_jwt_token() — token verification and error handling."""

    # 32+ characters required by pyjwt to avoid InsecureKeyLengthWarning
    SECRET = "test-jwt-secret-key-32-chars-min!!!!!!"

    def test_verify_valid_token_returns_payload(self) -> None:
        """Valid token returns decoded payload."""
        from utils.crypto import create_jwt_token, verify_jwt_token

        payload = {"sub": str(uuid4()), "scope": "read"}
        token = create_jwt_token(payload, self.SECRET, timedelta(hours=1))
        decoded = verify_jwt_token(token, self.SECRET)
        assert decoded["sub"] == payload["sub"]
        assert decoded["scope"] == "read"

    def test_expired_token_raises_authentication_error(self) -> None:
        """Expired token raises AuthenticationError."""
        from utils.crypto import create_jwt_token, verify_jwt_token

        token = create_jwt_token(
            data={"sub": str(uuid4())},
            secret=self.SECRET,
            expires_delta=timedelta(seconds=0),  # already expired
        )
        sleep(0.05)  # guarantee expiry
        with pytest.raises(AuthenticationError, match="Token expired"):
            verify_jwt_token(token, self.SECRET)

    def test_wrong_secret_raises_authentication_error(self) -> None:
        """Token signed with wrong secret raises AuthenticationError."""
        from utils.crypto import create_jwt_token, verify_jwt_token

        token = create_jwt_token(
            data={"sub": str(uuid4())},
            secret="a" * 32,
            expires_delta=timedelta(hours=1),
        )
        with pytest.raises(AuthenticationError, match="Invalid token"):
            verify_jwt_token(token, "b" * 32)

    def test_malformed_token_raises_authentication_error(self) -> None:
        """Garbage token string raises AuthenticationError."""
        from utils.crypto import verify_jwt_token

        with pytest.raises(AuthenticationError, match="Invalid token"):
            verify_jwt_token("not.a.jwt", self.SECRET)

    def test_empty_token_raises_authentication_error(self) -> None:
        """Empty token string raises AuthenticationError."""
        from utils.crypto import verify_jwt_token

        with pytest.raises(AuthenticationError, match="Invalid token"):
            verify_jwt_token("", self.SECRET)

    def test_wrong_algorithm_rejected(self) -> None:
        """Token with wrong algorithm (HS512 vs expected HS256) raises."""
        import jwt as pyjwt

        from utils.crypto import verify_jwt_token

        # HS512 requires 64+ byte keys to avoid InsecureKeyLengthWarning
        long_key = "a" * 64
        token = pyjwt.encode(
            {"sub": str(uuid4()), "exp": 9999999999},
            long_key,
            algorithm="HS512",
        )
        # verify_jwt_token only accepts HS256
        with pytest.raises(AuthenticationError, match="Invalid token"):
            verify_jwt_token(token, long_key)

    def test_aud_claim_rejected_without_audience_param(self) -> None:
        """Token with 'aud' claim is rejected because verify_jwt_token
        does not pass an audience parameter — pyjwt validates audience
        by default when the claim is present."""
        from utils.crypto import create_jwt_token, verify_jwt_token

        token = create_jwt_token(
            data={"sub": str(uuid4()), "aud": "my-api"},
            secret=self.SECRET,
            expires_delta=timedelta(hours=1),
        )
        with pytest.raises(AuthenticationError, match="Invalid token"):
            verify_jwt_token(token, self.SECRET)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def generate_api_key_for_test() -> str:
    """Generate a throwaway API key for test use (oz_test_ prefix)."""
    from utils.crypto import generate_api_key

    return generate_api_key(prefix="oz_test_")


def deadbeef_salt() -> str:
    """Return a deterministic 32-hex-char salt."""
    return "d" * 32
