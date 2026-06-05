#!/usr/bin/env python3
"""Generate a development API key with salted SHA-256 hash.

Outputs a raw API key (``mg_test_`` prefix) and the corresponding salted
SHA-256 hash.  Store the **hash** in your database; give the **raw key** to
the developer / client.

Usage:

    python scripts/create_dev_key.py
    python scripts/create_dev_key.py --prefix mg_dev_

Example output::

    Raw key:   mg_test_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6
    Hash:      $6$abc123def456...$a1b2c3d4...
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import sys


def derive_salt(length: int = 16) -> bytes:
    """Generate a cryptographically random salt.

    Args:
        length: Number of random bytes.

    Returns:
        A random byte string.
    """
    return os.urandom(length)


def hash_api_key(raw_key: str, salt: bytes) -> str:
    """Return a salted SHA-256 hash of *raw_key* in Unix crypt-compatible form.

    The output format is ``$6$<salt_hex>$<hash_hex>`` so that it can be
    stored alongside existing password-hashing schemes if needed.

    Args:
        raw_key: The plain-text API key to hash.
        salt: Random salt bytes.

    Returns:
        A string of the form ``$6$<64-char hex salt>$<64-char hex hash>``.
    """
    d = hashlib.pbkdf2_hmac(
        "sha256",
        raw_key.encode("utf-8"),
        salt,
        iterations=600_000,
        dklen=32,
    )
    return f"$6${salt.hex()}${d.hex()}"


def generate_api_key(prefix: str = "mg_test_", byte_length: int = 32) -> str:
    """Generate a cryptographically random API key string.

    Args:
        prefix: String prepended to the random part (e.g. ``mg_test_``).
        byte_length: Number of random bytes (output will be ``2 * byte_length``
            hex characters long).

    Returns:
        A key like ``mg_test_a1b2c3d4...``.
    """
    return prefix + secrets.token_hex(byte_length)


def main() -> None:
    """CLI entry point.  Parse args, generate key, print result."""
    parser = argparse.ArgumentParser(
        description="Generate a development API key with salted SHA-256 hash."
    )
    parser.add_argument(
        "--prefix",
        default="mg_test_",
        help="Prefix for the generated key (default: mg_test_).",
    )
    parser.add_argument(
        "--byte-length",
        type=int,
        default=32,
        help="Number of random bytes for the key (default: 32).",
    )
    args = parser.parse_args()

    raw_key = generate_api_key(prefix=args.prefix, byte_length=args.byte_length)
    salt = derive_salt()
    key_hash = hash_api_key(raw_key, salt)

    print(f"Raw key:   {raw_key}")
    print(f"Hash:      {key_hash}")

    sys.stdout.flush()


if __name__ == "__main__":
    main()
