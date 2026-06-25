"""E2E test fixtures — reuses integration testcontainers stack.

Pulls session-scoped fixtures (``engine``, ``redis_client``) from the
integration conftest so e2e tests share the same PostgreSQL + Redis
testcontainers as integration tests.  No additional container overhead.

.. note::

    ``pytest_plugins`` is **not** defined here because pytest 9 no longer
    supports declaring plugins inside a non-top-level conftest.  E2E tests
    that need integration fixtures should import them explicitly from
    ``tests.integration.conftest``.
"""

from __future__ import annotations

# E2E tests that need integration fixtures must import them directly,
# e.g.:
#   from tests.integration.conftest import engine, redis_client, app  # noqa: F401
