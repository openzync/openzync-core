# Test Infrastructure

| | |
|---|---|
| **Document** | 14-testing/02-test-infrastructure.md |
| **Phase** | 5 — Hardening |
| **Author** | Technical Writing |
| **Status** | Draft |

---

## 1. conftest.py Structure

The `conftest.py` hierarchy follows the monorepo domain structure:

```
tests/
├── conftest.py                 # Shared: app, DB, Redis, HTTP client, auth
├── factories/                   # Factory functions for test data
│   ├── __init__.py
│   ├── user_factory.py
│   ├── session_factory.py
│   ├── episode_factory.py
│   └── fact_factory.py
├── fixtures/                    # Static test data files
│   ├── llm_responses/
│   │   ├── entities.json
│   │   ├── facts.json
│   │   └── classifications.json
│   ├── datasets/                # Golden datasets (see 03-golden-datasets.md)
│   │   ├── entity_extraction_v1.jsonl
│   │   ├── fact_extraction_v1.jsonl
│   │   ├── classification_v1.jsonl
│   │   ├── structured_extraction_v1.jsonl
│   │   ├── retrieval_queries_v1.jsonl
│   │   └── cross_tenant_matrix.json
│   └── sample_conversations.json
├── unit/
│   ├── conftest.py              # Mocks only, no containers
│   ├── test_rrf.py
│   └── test_pagination.py
├── integration/
│   ├── conftest.py              # Domain-specific fixtures
│   ├── test_memory_integration.py
│   ├── test_context_integration.py
│   └── test_graph_integration.py
├── e2e/
│   ├── conftest.py              # Docker Compose health checks
│   └── test_main_flow.py
├── security/
│   ├── conftest.py
│   └── test_cross_tenant.py
├── performance/
│   ├── locustfile.py
│   └── seed_data.py
└── evals/
    ├── conftest.py
    └── test_entity_extraction_eval.py
```

### 1.1 Root `tests/conftest.py`

Shared across all test types. Provides the slow fixtures (containers, app instance) and fast fixtures (auth headers, factories).

```python
# tests/conftest.py
import uuid
import pytest
from collections.abc import AsyncGenerator
from httpx import ASGITransport, AsyncClient
from app.main import create_app
from app.core.config import Settings
from app.core.db import AsyncSessionLocal
from app.dependencies.auth import APIKeyScope

@pytest.fixture(scope="session")
def settings() -> Settings:
    """Test settings override. Override env vars in pytest.ini or pyproject.toml."""
    return Settings(
        DATABASE_URL="postgresql+asyncpg://test:test@localhost:15432/testdb",
        REDIS_URL="redis://localhost:16379/0",
        FALKORDB_URL="redis://localhost:16380",
        LLM_BACKEND="mock",
        OPENAI_API_KEY="sk-test-fake-key",
        SECRET_KEY="test-secret-key-not-for-production",
        LOG_LEVEL="ERROR",
    )

@pytest.fixture(scope="session")
def app(settings: Settings):
    """Create a fresh FastAPI app for the test session."""
    return create_app(settings=settings)

@pytest.fixture
async def db_session(app) -> AsyncGenerator[AsyncSession, None]:
    """Get a fresh DB session for each test (function-scoped)."""
    async with AsyncSessionLocal() as session:
        yield session
        await session.rollback()

@pytest.fixture
def organization_id() -> uuid.UUID:
    """Return a deterministic org ID for tests."""
    return uuid.UUID("00000000-0000-0000-0000-000000000001")

@pytest.fixture
def user_id() -> uuid.UUID:
    """Return a deterministic user ID for tests."""
    return uuid.UUID("00000000-0000-0000-0000-000000000002")

@pytest.fixture(scope="session")
def api_key() -> str:
    """A test API key with known hash. The corresponding key_hash is pre-seeded."""
    return "mg_test_abc123def456ghi789"

@pytest.fixture
async def async_client(app, api_key: str) -> AsyncClient:
    """HTTP client pre-configured with auth headers."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {api_key}"
        yield client
```

---

## 2. Fixture Scoping Rules

| Scope | When | Examples |
|---|---|---|
| `session` | Once per test session | `app`, `settings`, `postgres_container`, `redis_container`, `falkordb_container`, `api_key` |
| `module` | Once per test module | `mock_llm_responses` (loaded from JSON), `golden_dataset` (loaded from file) |
| `function` | Fresh for each test (default) | `db_session`, `async_client`, `organization_id`, `user_id`, `mocked_repo` |

**Guideline:** Use `session` scope for anything that starts a Docker container or reads a large file from disk. Use `function` scope by default. Only use `module` scope if a fixture is expensive to construct but varies between modules.

```python
# CORRECT: session-scoped container, function-scoped DB session
@pytest.fixture(scope="session")
def redis_container():
    with RedisContainer("redis:7-alpine") as redis:
        yield redis

@pytest.fixture
async def redis_client(redis_container):
    """Function-scoped because we want a clean state per test."""
    client = redis.from_url(redis_container.get_url())
    await client.flushdb()
    yield client
    await client.aclose()
```

---

## 3. Testcontainers

### 3.1 PostgreSQL with pgvector

```python
# tests/integration/conftest.py
import pytest
from testcontainers.postgres import PostgresContainer

@pytest.fixture(scope="session")
def postgres_container():
    """PostgreSQL 15 with pgvector pre-installed."""
    with PostgresContainer("pgvector/pgvector:pg15") as pg:
        pg.with_bind_ports(5432, 15432)  # Fixed port for predictability
        yield pg

@pytest.fixture(scope="session")
def database_url(postgres_container) -> str:
    """Construct the async DSN from the container."""
    return (
        f"postgresql+asyncpg://"
        f"{postgres_container.USERNAME}:{postgres_container.PASSWORD}"
        f"@{postgres_container.get_container_host_ip()}"
        f":{postgres_container.get_exposed_port(5432)}"
        f"/{postgres_container.DBNAME}"
    )

@pytest.fixture(scope="session")
async def db_engine(database_url: str):
    """Create engine once, run migrations."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from app.core.db import Base

    engine = create_async_engine(database_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()
```

### 3.2 Redis

```python
@pytest.fixture(scope="session")
def redis_container():
    with RedisContainer("redis:7-alpine") as redis:
        redis.with_bind_ports(6379, 16379)
        yield redis
```

### 3.3 FalkorDB

```python
@pytest.fixture(scope="session")
def falkordb_container():
    """FalkorDB, uses Redis wire protocol on port 6380."""
    from testcontainers.redis import RedisContainer
    # FalkorDB listens on the same Redis protocol but uses a different image
    import docker
    client = docker.from_env()
    container = client.containers.run(
        "falkordb/falkordb:latest",
        ports={"6380/tcp": 16380},
        detach=True,
        remove=True,
    )
    # Wait for readiness
    import time, redis
    r = redis.Redis(host="localhost", port=16380)
    for _ in range(30):
        try:
            r.ping()
            break
        except redis.ConnectionError:
            time.sleep(1)
    yield container
    container.stop()
```

**Alternative:** If the `testcontainers` library adds FalkorDB support natively, switch to:

```python
from testcontainers.falkordb import FalkorDBContainer

@pytest.fixture(scope="session")
def falkordb_container():
    with FalkorDBContainer("falkordb/falkordb:latest") as fdb:
        yield fdb
```

### 3.4 Neo4j (alternative graph backend)

```python
@pytest.fixture(scope="session")
def neo4j_container():
    from testcontainers.neo4j import Neo4jContainer

    with Neo4jContainer("neo4j:5-community") as neo4j:
        neo4j.with_env("NEO4J_AUTH", "neo4j/testpassword")
        yield neo4j
```

### 3.5 Container Reuse

Enable testcontainers reuse to avoid restarting containers across test sessions:

```python
# conftest.py or pytest.ini
# export TESTCONTAINERS_REUSE_ENABLE=true
#
# OR in Python:
@pytest.fixture(scope="session")
def postgres_container():
    with PostgresContainer("pgvector/pgvector:pg15", reuse=True) as pg:
        yield pg
```

**⚠️ Warning:** Reuse can cause state leakage between sessions. Only enable in development, never in CI. CI always starts fresh containers.

---

## 4. Mock LLM Suite

### 4.1 `MockLLM` Class

```python
# tests/mocks/llm.py
import json
import hashlib
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "llm_responses"

class MockLLM:
    """Replaces openai.AsyncClient with deterministic canned responses.

    Responses are matched by prompt hash prefix. If no match is found,
    the mock returns a sensible default or raises, depending on strict_mode.
    """

    def __init__(self, strict_mode: bool = True) -> None:
        self._responses: dict[str, Any] = {}
        self._call_history: list[dict] = []
        self.strict_mode = strict_mode
        self._load_fixtures()

    def _load_fixtures(self) -> None:
        """Load canned responses from JSON fixture files."""
        for fixture_file in FIXTURES_DIR.glob("*.json"):
            key = fixture_file.stem  # e.g. "entities", "facts", "classifications"
            with open(fixture_file) as f:
                self._responses[key] = json.load(f)

    def _compute_prompt_hash(self, messages: list[dict]) -> str:
        """Hash the last user message to determine which response to return."""
        content = " ".join(
            m["content"] for m in messages if m["role"] == "user"
        )
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    async def chat_completions_create(
        self, messages: list[dict], **kwargs: Any
    ) -> dict:
        """Mock replacement for openai.AsyncClient.chat.completions.create()."""
        call = {
            "messages": messages,
            "kwargs": kwargs,
            "prompt_hash": self._compute_prompt_hash(messages),
        }
        self._call_history.append(call)

        # Match by fixture key embedded in the system prompt
        system_prompt = next(
            (m["content"] for m in messages if m["role"] == "system"), ""
        )

        for fixture_key, response in self._responses.items():
            if fixture_key in system_prompt.lower():
                return self._to_openai_format(response)

        if self.strict_mode:
            raise ValueError(
                f"No canned response matched. System prompt key: "
                f"'{system_prompt[:80]}...'"
            )
        # Default fallback
        return self._to_openai_format({"choices": [{"text": "{}"}]})

    def _to_openai_format(self, data: Any) -> dict:
        """Wrap data in OpenAI-compatible response format."""
        return {
            "choices": [{"message": {"content": json.dumps(data)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }

    @property
    def call_count(self) -> int:
        return len(self._call_history)

    def reset(self) -> None:
        self._call_history.clear()


# Dependency injection fixture
@pytest.fixture
def mock_llm():
    llm = MockLLM(strict_mode=True)
    yield llm
    llm.reset()


@pytest.fixture
def override_llm_dependency(app, mock_llm):
    """Replace the real LLM client dependency with MockLLM in the app."""
    from app.dependencies import get_llm_client

    app.dependency_overrides[get_llm_client] = lambda: mock_llm
    yield
    app.dependency_overrides.clear()
```

### 4.2 Fixture Response Files

#### `tests/fixtures/llm_responses/entities.json`

```json
{
  "entities": [
    {"name": "Alice", "type": "Person", "summary": "Software engineer preferring Python"},
    {"name": "Bob", "type": "Person", "summary": "Product manager at Acme Corp"}
  ],
  "relationships": [
    {"subject": "Alice", "predicate": "works_with", "object": "Bob"}
  ]
}
```

#### `tests/fixtures/llm_responses/facts.json`

```json
{
  "facts": [
    {
      "content": "Alice prefers Python over JavaScript",
      "subject": "Alice",
      "predicate": "prefers",
      "object": "Python",
      "confidence": 0.95
    }
  ]
}
```

#### `tests/fixtures/llm_responses/classifications.json`

```json
{
  "intent": "question",
  "emotion": "neutral",
  "valence": "neutral",
  "arousal": "low"
}
```

### 4.3 Real LLM Client Guard

```python
# app/dependencies/llm.py
import os

def get_llm_client():
    """Return the configured LLM client. In tests, this is overridden."""
    if os.environ.get("LLM_BACKEND") == "mock":
        from tests.mocks.llm import MockLLM
        return MockLLM(strict_mode=False)
    # ... real client setup ...
```

**Test-time guard to prevent accidental real API calls:**

```python
# conftest.py — fail if any test tries to use the real OpenAI client
@pytest.fixture(autouse=True)
def block_real_llm(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-blocked")
    monkeypatch.setenv("LLM_BACKEND", "mock")
```

---

## 5. Database Factories

All factories create rows in the database with sensible defaults, then return the ORM object. Override any field via `**kwargs`.

### 5.1 Factory Base Pattern

```python
# tests/factories/_base.py
import uuid
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession

class BaseFactory:
    """Base factory with async insert pattern."""

    @classmethod
    async def _create(cls, db: AsyncSession, model_class, defaults: dict, overrides: dict) -> object:
        data = {**defaults, **overrides}
        instance = model_class(**data)
        db.add(instance)
        await db.flush()
        await db.refresh(instance)
        return instance
```

### 5.2 User Factory

```python
# tests/factories/user_factory.py
import uuid
from app.models.user import User
from tests.factories._base import BaseFactory

class UserFactory(BaseFactory):
    @classmethod
    async def create(
        cls,
        db: AsyncSession,
        organization_id: uuid.UUID,
        **overrides,
    ) -> User:
        return await cls._create(db, User, {
            "id": uuid.uuid4(),
            "organization_id": organization_id,
            "external_id": f"user_{uuid.uuid4().hex[:8]}",
            "name": "Test User",
            "email": "test@example.com",
            "metadata": {},
        }, overrides)

    @classmethod
    async def create_batch(
        cls,
        db: AsyncSession,
        organization_id: uuid.UUID,
        count: int,
        **shared_overrides,
    ) -> list[User]:
        return [
            await cls.create(db, organization_id, **shared_overrides)
            for _ in range(count)
        ]
```

### 5.3 Session Factory

```python
# tests/factories/session_factory.py
import uuid
from app.models.session import Session

class SessionFactory(BaseFactory):
    @classmethod
    async def create(
        cls,
        db: AsyncSession,
        user_id: uuid.UUID,
        **overrides,
    ) -> Session:
        return await cls._create(db, Session, {
            "id": uuid.uuid4(),
            "user_id": user_id,
            "external_id": f"session_{uuid.uuid4().hex[:8]}",
            "metadata": {},
        }, overrides)
```

### 5.4 Episode Factory

```python
# tests/factories/episode_factory.py
import uuid
from datetime import datetime, timezone
from app.models.episode import Episode

class EpisodeFactory(BaseFactory):
    @classmethod
    async def create(
        cls,
        db: AsyncSession,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
        **overrides,
    ) -> Episode:
        return await cls._create(db, Episode, {
            "id": uuid.uuid4(),
            "session_id": session_id,
            "user_id": user_id,
            "role": "user",
            "content": "This is a test message.",
            "metadata": {},
            "created_at": datetime.now(timezone.utc),
        }, overrides)

    @classmethod
    async def create_conversation(
        cls,
        db: AsyncSession,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
        turns: int = 5,
    ) -> list[Episode]:
        """Create a multi-turn conversation with alternating roles."""
        episodes = []
        for i in range(turns):
            role = "user" if i % 2 == 0 else "assistant"
            ep = await cls.create(
                db, session_id, user_id,
                role=role,
                content=f"Test message {i+1} from {role}.",
            )
            episodes.append(ep)
        return episodes
```

### 5.5 Fact Factory

```python
# tests/factories/fact_factory.py
import uuid
from datetime import datetime, timezone
from app.models.fact import Fact

class FactFactory(BaseFactory):
    @classmethod
    async def create(
        cls,
        db: AsyncSession,
        user_id: uuid.UUID,
        **overrides,
    ) -> Fact:
        return await cls._create(db, Fact, {
            "id": uuid.uuid4(),
            "user_id": user_id,
            "content": "A test fact about the user.",
            "subject": "user",
            "predicate": "likes",
            "object": "testing",
            "confidence": 0.95,
            "valid_from": datetime.now(timezone.utc),
        }, overrides)
```

### 5.6 Bulk Factories for Performance Test Seeding

```python
# tests/factories/bulk.py
"""High-performance batch insertion for seeding load test data.
Uses raw SQL bulk inserts (not ORM) for speed."""

from sqlalchemy import text

async def seed_performance_data(
    db,
    num_users: int = 1000,
    sessions_per_user: int = 5,
    episodes_per_session: int = 10,
    facts_per_session: int = 2,
) -> dict:
    """Insert test data in bulk and return IDs for reference."""
    # Use raw INSERT ... VALUES (...), (...), ...
    # Wrapped in a single transaction
    async with db.begin():
        # ... bulk insert statements ...
        pass
```

---

## 6. HTTP Client Fixture

### 6.1 Standard Authenticated Client

```python
@pytest.fixture
async def async_client(app, api_key: str) -> AsyncClient:
    """Authenticated HTTP client using ASGITransport (no server needed)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {api_key}"
        yield client
```

### 6.2 Unauthenticated Client (for auth-bypass tests)

```python
@pytest.fixture
async def anon_client(app) -> AsyncClient:
    """HTTP client without auth headers — should get 401."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
```

### 6.3 Admin Super-Key Client

```python
@pytest.fixture
def super_key() -> str:
    return "mg_live_super_admin_key_789"

@pytest.fixture
async def admin_client(app, super_key: str) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {super_key}"
        yield client
```

### 6.4 Tenant-Specific Clients

```python
# tests/security/conftest.py
import pytest

TENANTS = {
    "A": {"org_id": "org-a-0001", "api_key": "mg_test_tenant_a_key"},
    "B": {"org_id": "org-b-0002", "api_key": "mg_test_tenant_b_key"},
    "C": {"org_id": "org-c-0003", "api_key": "mg_test_tenant_c_key"},
}

@pytest.fixture(params=TENANTS.keys())
def tenant_id(request) -> str:
    return request.param

@pytest.fixture
def tenant_config(tenant_id: str) -> dict:
    return TENANTS[tenant_id]

@pytest.fixture
async def tenant_client(app, tenant_config: dict) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {tenant_config['api_key']}"
        yield client
```

---

## 7. Test Isolation Guarantees

| Concern | Mechanism |
|---|---|
| DB state between tests | Each test gets a fresh `db_session` with `rollback()` in teardown |
| Redis state between tests | `flushdb()` in `redis_client` teardown |
| FalkorDB state between tests | `flushall()` in `falkordb_client` teardown |
| App dependency overrides | `app.dependency_overrides.clear()` in each test's teardown |
| Environment variables | `monkeypatch` (pytest built-in) for per-test env overrides |
| Logging | `caplog` fixture for capturing log output in assertions |
| Time | `freezegun` for tests that depend on `datetime.now()` |
| Async event loop | `pytest-asyncio` manages loop lifecycle per function |

---

## 8. Running Tests

```bash
# All unit tests (parallel)
pytest tests/unit/ -n auto --cov=app --cov-report=term

# Integration tests (sequential, testcontainers)
pytest tests/integration/ --timeout=120 -v

# Single integration test
pytest tests/integration/test_memory_integration.py -k "test_ingest_and_retrieve" -v --timeout=60

# E2E tests (Docker Compose must be up)
pytest tests/e2e/ --timeout=300 -v

# Security tests
pytest tests/security/ -v

# Performance tests
locust -f tests/performance/locustfile.py --headless -u 100 -r 10 -H http://localhost:8000

# All non-performance tests
pytest -m "not perf and not eval"

# Coverage report
pytest --cov=app --cov-report=html --cov-report=term
```
