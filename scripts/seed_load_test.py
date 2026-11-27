#!/usr/bin/env python3
"""Seed script for the Locust load test (G1.4).

Bootstraps the platform (``POST /v1/auth/setup``), logs in as root,
creates a user + project, and seeds episodes + facts for context
assembly load testing.

Usage:
    python scripts/seed_load_test.py --api-url http://localhost:8000

Environment variables:
    OZ_LOAD_TEST_ROOT_PASSWORD: Root password for one-time setup
        (default: "load-test-root-123"). Ignored when setup ran before.
    OZ_LOAD_TEST_USER: User external ID (default: "load_test_user").
    OZ_LOAD_TEST_PROJECT: Project name (default: "Load Test Project").
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os

import httpx

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_client(base_url: str, token: str | None = None) -> httpx.AsyncClient:
    """Create an authenticated HTTP client."""
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.AsyncClient(base_url=base_url, headers=headers, timeout=30)


async def bootstrap_platform(client: httpx.AsyncClient) -> None:
    """One-time platform setup — 403 when setup already ran (tolerated)."""
    resp = await client.post(
        "/v1/auth/setup",
        json={
            "password": os.environ.get(
                "OZ_LOAD_TEST_ROOT_PASSWORD", "load-test-root-123"
            ),
            "org_name": "Platform",
        },
    )
    if resp.status_code == 403:
        logger.info("Platform already set up — continuing.")
        return
    resp.raise_for_status()
    logger.info("Platform setup complete.")


async def login_root(client: httpx.AsyncClient) -> str:
    """Log in as root and return the access token."""
    resp = await client.post(
        "/v1/auth/login",
        json={
            "email": "root",
            "password": os.environ.get(
                "OZ_LOAD_TEST_ROOT_PASSWORD", "load-test-root-123"
            ),
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


async def create_user(client: httpx.AsyncClient, user_ext_id: str) -> str:
    """Create a test user and return the user UUID."""
    resp = await client.post("/v1/users", json={"external_id": user_ext_id})
    resp.raise_for_status()
    user_id = resp.json()["id"]
    logger.info("User created: %s (external_id: %s)", user_id, user_ext_id)
    return user_id


async def create_project(client: httpx.AsyncClient, name: str) -> str:
    """Create a test project and return the project UUID."""
    resp = await client.post("/v1/projects", json={"name": name})
    resp.raise_for_status()
    project_id = resp.json()["id"]
    logger.info("Project created: %s (name: %s)", project_id, name)
    return project_id


async def create_session(
    client: httpx.AsyncClient, project_id: str, external_id: str
) -> None:
    """Create a session external ID for memory/fact ingestion."""
    resp = await client.post(
        f"/v1/projects/{project_id}/sessions",
        json={"external_id": external_id},
    )
    resp.raise_for_status()


async def seed_episodes(
    client: httpx.AsyncClient,
    project_id: str,
    session_id: str,
    count: int = 100,
) -> None:
    """Seed episodes (conversation turns) into the project."""
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
            f"/v1/projects/{project_id}/memory",
            data={
                "data": json.dumps(
                    {"session_id": session_id, "messages": messages}
                )
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


async def seed_facts(
    client: httpx.AsyncClient,
    project_id: str,
    session_id: str,
    count: int = 500,
) -> None:
    """Seed facts (triples) into the project."""
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
            f"/v1/projects/{project_id}/facts",
            json={"session_id": session_id, "facts": facts},
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
    project_name = os.environ.get("OZ_LOAD_TEST_PROJECT", "Load Test Project")
    session_ext_id = "load_test_session"

    # Step 1: One-time platform bootstrap (tolerates 403 when already set up)
    anon_client = _make_client(args.api_url)
    await bootstrap_platform(anon_client)
    await anon_client.aclose()

    # Step 2: Root login → admin client
    anon_client = _make_client(args.api_url)
    token = await login_root(anon_client)
    await anon_client.aclose()
    auth_client = _make_client(args.api_url, token)

    try:
        # Step 3: Create user + project + session
        user_id = await create_user(auth_client, user_ext_id)
        project_id = await create_project(auth_client, project_name)
        await create_session(auth_client, project_id, session_ext_id)

        # Export for Locust
        print("\nExport for Locust:")
        print(f"  export OZ_LOAD_TEST_TOKEN={token[:16]}...")
        print(f"  export OZ_LOAD_TEST_USER_ID={user_id}")
        print(f"  export OZ_LOAD_TEST_PROJECT_ID={project_id}")
        print()

        # Step 4: Seed episodes
        logger.info("Seeding episodes...")
        await seed_episodes(auth_client, project_id, session_ext_id, count=100)

        # Step 5: Seed facts
        logger.info("Seeding facts...")
        await seed_facts(auth_client, project_id, session_ext_id, count=500)

        logger.info("\n✅ Load test data seeded successfully!")
        logger.info(
            "Run Locust with the exported token/project against "
            "tests/performance/locustfile.py"
        )

    finally:
        await auth_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
