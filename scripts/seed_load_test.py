#!/usr/bin/env python3
"""Seed script for the Locust load test (G1.4).

Creates a test org, API key, user, and seeds 500 facts + 100 episodes
for context assembly load testing.

Usage:
    python scripts/seed_load_test.py --api-url http://localhost:8000

Environment variables:
    OZ_LOAD_TEST_ORG: Organization name (default: "Load Test Org")
    OZ_LOAD_TEST_USER: User external ID (default: "load_test_user")
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time

import httpx

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_client(base_url: str, api_key: str | None = None) -> httpx.AsyncClient:
    """Create an authenticated HTTP client."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return httpx.AsyncClient(base_url=base_url, headers=headers, timeout=30)


async def bootstrap_org(client: httpx.AsyncClient) -> dict:
    """Create a test org and return its API key."""
    resp = await client.post(
        "/admin/organizations",
        json={"name": os.environ.get("OZ_LOAD_TEST_ORG", "Load Test Org")},
    )
    resp.raise_for_status()
    data = resp.json()
    logger.info("Org created: %s (API key: %s...)",
                data["organization_id"], data["api_key"][:12])
    return data


async def create_user(client: httpx.AsyncClient, user_ext_id: str) -> str:
    """Create a test user and return the user UUID."""
    resp = await client.post("/v1/users", json={"external_id": user_ext_id})
    resp.raise_for_status()
    user_id = resp.json()["id"]
    logger.info("User created: %s (external_id: %s)", user_id, user_ext_id)
    return user_id


async def seed_episodes(client: httpx.AsyncClient, user_id: str, count: int = 100) -> None:
    """Seed episodes (conversation turns) for the test user."""
    topics = [
        "python programming", "data structures", "machine learning",
        "database indexes", "API design", "testing strategies",
        "deployment pipelines", "security", "performance",
        "error handling", "async patterns", "caching strategies",
        "logging best practices", "configuration management",
        "container orchestration",
    ]

    batch_size = 10
    seeded = 0

    for batch_start in range(0, count, batch_size):
        batch_end = min(batch_start + batch_size, count)
        messages = []
        for i in range(batch_start, batch_end):
            topic = topics[i % len(topics)]
            messages.append(
                {"role": "user", "content": f"Tell me about {topic} in detail."}
            )
            messages.append(
                {
                    "role": "assistant",
                    "content": (
                        f"Here is detailed information about {topic}. "
                        f"{topic.capitalize()} is important for building "
                        f"robust systems. Key concepts include proper "
                        f"abstraction, interface design, and testing."
                    ),
                }
            )

        resp = await client.post(
            f"/v1/users/{user_id}/memory",
            json={
                "session_id": "load_test_session",
                "messages": messages,
            },
        )
        if resp.status_code == 202:
            seeded += len(messages)
            logger.info("Seeded %d/%d episodes...", seeded, count * 2)
        else:
            logger.warning("Seed failed at %d: %s", batch_start, resp.text)

        # Small delay to avoid overwhelming the server
        await asyncio.sleep(0.1)

    logger.info("Episode seeding complete: %d episodes", seeded)


async def seed_facts(client: httpx.AsyncClient, user_id: str, count: int = 500) -> None:
    """Seed facts (triples) for the test user."""
    fact_templates = [
        ("Python", "is a", "dynamically-typed programming language"),
        ("Python", "supports", "multiple programming paradigms"),
        ("PostgreSQL", "is an", "object-relational database system"),
        ("Redis", "is an", "in-memory data structure store"),
        ("FastAPI", "is a", "modern Python web framework"),
        ("Docker", "is a", "containerization platform"),
        ("Kubernetes", "orchestrates", "container deployments"),
        ("Machine Learning", "requires", "large datasets for training"),
        ("API Design", "should follow", "REST or GraphQL principles"),
        ("Testing", "includes", "unit, integration, and e2e tests"),
    ]

    batch_size = 50
    seeded = 0

    for batch_start in range(0, count, batch_size):
        batch_end = min(batch_start + batch_size, count)
        facts = []
        for i in range(batch_start, batch_end):
            subj, pred, obj = fact_templates[i % len(fact_templates)]
            facts.append({
                "subject": subj,
                "predicate": pred,
                "object": obj,
                "content": f"{subj} {pred} {obj}",
                "confidence": 0.95,
            })

        resp = await client.post(
            f"/v1/users/{user_id}/facts",
            json={"facts": facts},
        )
        if resp.status_code == 202:
            seeded += len(facts)
            logger.info("Seeded %d/%d facts...", seeded, count)
        else:
            logger.warning("Fact seed failed at %d: %s", batch_start, resp.text)

        await asyncio.sleep(0.1)

    logger.info("Fact seeding complete: %d facts", seeded)


# ── Main ────────────────────────────────────────────────────────────────────


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed data for context assembly load testing"
    )
    parser.add_argument(
        "--api-url",
        default="http://localhost:8000",
        help="Base URL of the OpenZync API",
    )
    args = parser.parse_args()

    user_ext_id = os.environ.get("OZ_LOAD_TEST_USER", "load_test_user")

    # Step 1: Bootstrap org
    anon_client = _make_client(args.api_url)
    org_data = await bootstrap_org(anon_client)
    await anon_client.aclose()

    api_key = org_data["api_key"]
    auth_client = _make_client(args.api_url, api_key)

    try:
        # Step 2: Create user
        user_id = await create_user(auth_client, user_ext_id)

        # Export for Locust
        print(f"\nExport these for Locust:")
        print(f"  export OZ_LOAD_TEST_API_KEY={api_key}")
        print(f"  export OZ_LOAD_TEST_USER_ID={user_id}")
        print()

        # Step 3: Seed episodes
        logger.info("Seeding episodes...")
        await seed_episodes(auth_client, user_id, count=100)

        # Step 4: Seed facts
        logger.info("Seeding facts...")
        await seed_facts(auth_client, user_id, count=500)

        logger.info("\n✅ Load test data seeded successfully!")
        logger.info("Run Locust with:")
        logger.info(
            f"  OZ_LOAD_TEST_API_KEY={api_key[:16]}... "
            f"OZ_LOAD_TEST_USER_ID={user_id} "
            f"locust -f tests/performance/locustfile.py --headless "
            f"-u 10 -r 2 --run-time 5m --host {args.api_url}"
        )

    finally:
        await auth_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
