"""Locust load test for context assembly latency (G1.4).

Validates:
    p99 cold (first request) ≤ 1500ms
    p99 warm (subsequent requests) ≤ 300ms

Prerequisites:
    - API server running at TARGET_HOST (default http://localhost:8000)
    - Test org + API key bootstrapped via ``scripts/seed_load_test.py``
    - At least 500 facts + 100 episodes seeded for the test user

Usage:
    locust -f tests/performance/locustfile.py --headless \\
        -u 10 -r 2 --run-time 5m \\
        --host http://localhost:8000
"""

from __future__ import annotations

import os

from locust import FastHttpUser, between, events, task

# ═══════════════════════════════════════════════════════════════════════════════
# Configuration from environment
# ═══════════════════════════════════════════════════════════════════════════════

API_KEY: str = os.environ.get(
    "MG_LOAD_TEST_API_KEY", "mg_test_" + "a" * 64
)
"""Pre-provisioned API key for load testing."""

TEST_USER_ID: str = os.environ.get(
    "MG_LOAD_TEST_USER_ID", "00000000-0000-0000-0000-000000000001"
)
"""Pre-seeded user ID with 500+ facts and 100+ episodes."""

QUERIES: list[str] = [
    "python programming",
    "data structure",
    "machine learning",
    "database index",
    "API design",
    "testing strategy",
    "deployment pipeline",
    "security best practices",
    "performance optimization",
    "error handling",
]
"""Distinct queries to avoid cache pollution."""

WARMUP_QUERIES: list[str] = [
    "warmup query 1",
    "warmup query 2",
    "warmup query 3",
]
"""Queries used only during warmup phase to populate cache."""


# ═══════════════════════════════════════════════════════════════════════════════
# User behaviour
# ═══════════════════════════════════════════════════════════════════════════════


class ContextUser(FastHttpUser):
    """Simulates a user querying context assembly.

    Wait time between requests is 3-8 seconds to simulate realistic
    user interaction patterns.
    """

    wait_time = between(3, 8)

    def on_start(self) -> None:
        """Authenticate and warm up the cache."""
        self.client.headers.update({
            "Authorization": f"Bearer {API_KEY}",
        })

        # Warmup: populate Redis cache with a few context queries
        for q in WARMUP_QUERIES:
            with self.client.get(
                f"/v1/users/{TEST_USER_ID}/context",
                params={"query": q, "limit": 10},
                catch_response=True,
                name="[warmup]",
            ) as resp:
                if resp.status_code == 200:
                    resp.success()
                else:
                    resp.failure(f"Warmup failed: {resp.status_code}")

    @task
    def get_context(self) -> None:
        """GET /context with a random query — the primary load target."""
        import random

        query = random.choice(QUERIES)

        with self.client.get(
            f"/v1/users/{TEST_USER_ID}/context",
            params={"query": query, "limit": 20},
            catch_response=True,
            name="/context",
        ) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(
                    f"Expected 200, got {resp.status_code}: {resp.text[:200]}"
                )


# ═══════════════════════════════════════════════════════════════════════════════
# Event hooks for custom metrics
# ═══════════════════════════════════════════════════════════════════════════════


@events.init.add_listener
def on_locust_init(environment, **kwargs):  # noqa: ARG001
    """Print instructions when Locust starts."""
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║  Context Assembly Load Test (G1.4)                        ║")
    print("║                                                          ║")
    print("║  Targets:                                                ║")
    print("║    p99 cold  ≤ 1500ms (first request)                    ║")
    print("║    p99 warm  ≤ 300ms  (cached requests)                  ║")
    print("║                                                          ║")
    print("║  Prerequisites:                                          ║")
    print(f"║    TARGET_HOST = {environment.host or 'http://localhost:8000'}      ║")
    print("║    Seed data via scripts/seed_load_test.py               ║")
    print("╚══════════════════════════════════════════════════════════════╝")
