"""Root test configuration — shared fixtures for all test levels.

Fixtures requiring the application stack (``app``, ``async_client``, etc.)
live in ``tests/integration/conftest.py`` to avoid import-time failures when
the application hasn't been built yet (unit tests).
"""

from __future__ import annotations

import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# Auth helpers
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def test_api_key() -> str:
    """Return a synthetic API key for use in auth tests."""
    return "mg_test_" + "a" * 64
