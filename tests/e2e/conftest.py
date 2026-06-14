"""E2E test fixtures — reuses integration testcontainers stack.

Pulls session-scoped fixtures (``engine``, ``redis_client``) from the
integration conftest so e2e tests share the same PostgreSQL + Redis
testcontainers as integration tests.  No additional container overhead.
"""

from __future__ import annotations

# Re-export integration test fixtures so e2e tests get the same
# testcontainers-backed PostgreSQL, Redis, FastAPI app, and auth client.
pytest_plugins = [
    "tests.integration.conftest",
]
