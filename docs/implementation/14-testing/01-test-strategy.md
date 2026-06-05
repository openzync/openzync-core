# Test Strategy

| | |
|---|---|
| **Document** | 14-testing/01-test-strategy.md |
| **Phase** | 5 — Hardening |
| **Author** | Technical Writing |
| **Status** | Draft |

---

## 1. Overview

OpenZep follows a multi-layered test strategy inspired by the **test pyramid**, adapted for an LLM-enriched async system. The strategy balances speed, confidence, and coverage across six test levels.

The guiding principle: **fast tests run on every commit; slow tests gate merges; expensive tests run on a schedule.**

---

## 2. Test Levels

### 2.1 Unit Tests — `tests/unit/`

**Scope:** Pure logic, no I/O. Every test runs in-process with zero external dependencies.

**What they cover:**

| Package | What to test | Example |
|---|---|---|
| `services/` | Service methods with mocked repositories | `AgentService.create_session()` with `MockAgentRepository` |
| `core/rrf.py` | Reciprocal Rank Fusion merging logic | Correct weight calculation for ties |
| `core/pagination.py` | Cursor encoding/decoding, limit clamping | `encode_cursor()`, `decode_cursor()`, max-limit enforcement |
| `schemas/` | Pydantic validation rules | Field constraints, custom validators, default values |
| `dependencies/auth.py` | API key prefix detection, scope checking | `mg_live_` vs `mg_test_` routing |
| `core/config.py` | Environment variable parsing, defaults | `Settings` model validation |
| `core/exceptions.py` | Exception hierarchy, error code mapping | `NotFoundError` → `RESOURCE_NOT_FOUND` |
| `workers/tasks/*` | Task logic with mocked LLM clients | `extract_entities` with mock GPT response |

**Execution constraint:** Entire suite completes in **< 5 seconds**.

**Coverage target:** ≥ 85% line coverage on `services/` and `core/` packages.

### 2.2 Integration Tests — `tests/integration/`

**Scope:** Real databases, mocked LLMs. Tests verify that layers compose correctly.

**Infrastructure:**
- **PostgreSQL** (pgvector/pgvector:pg15) via `testcontainers`
- **Redis** (redis:7-alpine) via `testcontainers`
- **FalkorDB** (falkordb/falkordb:latest) via `testcontainers`
- **LLM calls** replaced with `MockLLM` returning canned responses

**What they cover:**

| Area | What to test | Example |
|---|---|---|
| All P0 endpoints (see §3) | Full HTTP request → response cycle | `POST /memory` → 202 → worker completes → fact exists in DB |
| Repository layer | SQLAlchemy queries against real PG | `UserRepository.get_by_id()` with actual rows |
| Graph operations | Graphiti with real FalkorDB | Entity creation, edge traversal |
| Cache integration | Redis cache-aside pattern | Context cache hit/miss behaviour |
| Worker tasks | ARQ job against real Redis | `extract_entities` task dispatched and processed |
| Auth middleware | API key validation against real DB | Valid key → 200, revoked key → 401 |
| Multi-tenancy | Cross-tenant query filtering (see [04-cross-tenant-test-matrix.md](./04-cross-tenant-test-matrix.md)) | Tenant A cannot see Tenant B's data |

**Execution constraint:** Entire suite completes in **< 60 seconds**.

**Coverage target:** All P0 endpoints have at least one happy-path integration test.

### 2.3 E2E Tests — `tests/e2e/`

**Scope:** Full Docker Compose stack. Tests exercise the complete system from API → worker → DBs → response.

**Infrastructure:**
- Full `docker compose -f infra/docker-compose.yml up`
- Real LLM backend (Ollama with a small model, or recorded API responses)
- Pre-seeded test data via migration scripts

**What they test:**
1. **Ingest → Enrich → Retrieve flow:** POST messages → wait for ARQ worker → GET context returns the expected facts
2. **Cross-service integration:** API → Redis queue → Worker → PG + FalkorDB → response consistency
3. **Dashboard API proxy:** Admin endpoints route correctly through the dashboard
4. **MCP server:** All 8 MCP tools respond correctly when called via stdio transport
5. **SDK smoke test:** Python SDK `client.memory.add()` → `client.memory.get()` returns non-empty context

**Execution constraint:** Entire suite completes in **< 5 minutes**.

**Trigger:** On merge to `main` branch. Not per-commit.

### 2.4 Performance Tests — `tests/performance/`

**Scope:** Load generation against a dedicated test environment. Verifies the non-functional requirements from SRS §6.1.

**Tool:** Locust (Python, team-familiar).

**Test scenarios:**
| Scenario | Target | Duration |
|---|---|---|
| Steady-state 100 req/s | p99 context < 300ms, error rate < 0.1% | 2 min |
| Peak 500 req/s (Phase 5 target) | p99 context < 300ms, error rate < 0.1% | 5 min |
| Cold cache context | p99 < 1500ms | 30 sec |
| Ingestion flood | Ack < 200ms p99 | 1 min |

See [05-load-test-spec.md](./05-load-test-spec.md) for detailed spec.

**Trigger:** Nightly (scheduled CI pipeline). On-demand for PRs flagged `perf:impact`.

### 2.5 Security Tests — `tests/security/`

**Scope:** Automated security verification.

**What they cover:**
1. **Cross-tenant isolation matrix** — exhaustive parametrized test (see [04-cross-tenant-test-matrix.md](./04-cross-tenant-test-matrix.md))
2. **Authentication bypass** — unauthenticated requests to every endpoint → 401
3. **Rate limiting** — rapid-fire requests trigger 429 after threshold
4. **Input validation boundary** — 64KB+ content → 413, malformed JSON → 422, SQL injection payloads → rejected
5. **Key rotation** — requests with old key after rotation → 401
6. **IDOR testing** — tenant A cannot access /admin endpoints without super_key scope

**Tooling:** `bandit` for SAST on Python code, custom parametrized pytest suite for runtime checks.

**Trigger:** On merge to `main`. Full suite runs in < 2 minutes.

### 2.6 Eval Tests — `tests/evals/`

**Scope:** LLM output quality measurement against golden datasets. These are **not** pass/fail in the traditional sense — they measure regression.

**What they cover:**

| Eval | Dataset | Metric | Threshold |
|---|---|---|---|
| Entity extraction quality | `entity_extraction_v1` | Exact match F1 | ≥ 0.85 |
| Fact extraction recall | `fact_extraction_v1` | Recall@5 | ≥ 0.80 |
| Dialog classification | `classification_v1` | Accuracy | ≥ 0.88 |
| Structured extraction | `structured_extraction_v1` | JSON schema compliance | 100% |

See [03-golden-datasets.md](./03-golden-datasets.md) for dataset specifications.

**Trigger:** Weekly (scheduled pipeline). Not per-commit. Results published to a dashboard.

---

## 3. P0 Endpoint Test Coverage Matrix

Every P0 endpoint requires: 1 unit test (service layer), 1 integration test (HTTP → DB), and 1 negative test (auth, validation, or not-found).

| Endpoint | Unit | Integration | Negative |
|---|---|---|---|
| `POST /v1/users` | ✅ | ✅ | Duplicate external_id → 409 |
| `GET /v1/users/{id}` | ✅ | ✅ | Unknown ID → 404 |
| `PATCH /v1/users/{id}` | ✅ | ✅ | Wrong tenant → 404 |
| `DELETE /v1/users/{id}` | ✅ | ✅ | Already deleted → 404 |
| `POST /v1/users/{id}/sessions` | ✅ | ✅ | Invalid user → 404 |
| `GET /v1/users/{id}/sessions` | ✅ | ✅ | Pagination edge cases |
| `GET /v1/users/{id}/sessions/{sid}` | ✅ | ✅ | Wrong tenant → 404 |
| `GET /v1/users/{id}/sessions/{sid}/messages` | ✅ | ✅ | Empty session → 200 [] |
| `POST /v1/users/{id}/memory` | ✅ | ✅ | Over 64KB content → 413 |
| `GET /v1/users/{id}/context` | ✅ | ✅ | Unknown user → 404 |
| `DELETE /v1/users/{id}/memory` | ✅ | ✅ | No memory → no-op |
| `POST /v1/users/{id}/facts` | ✅ | ✅ | Invalid triple → 422 |
| `GET /v1/users/{id}/facts` | ✅ | ✅ | Filter by confidence range |
| `DELETE /v1/users/{id}/facts/{fid}` | ✅ | ✅ | Wrong tenant → 404 |
| `GET /v1/users/{id}/graph/nodes` | ✅ | ✅ | No nodes → 200 [] |
| `GET /v1/users/{id}/graph/nodes/{nid}` | ✅ | ✅ | Wrong tenant → 404 |
| `GET /v1/users/{id}/search` | ✅ | ✅ | Empty query → 422 |

---

## 4. Coverage Targets

| Package | Minimum Line Coverage | Measured By |
|---|---|---|
| `services/` (all services) | 80% | `pytest --cov=services` |
| `repositories/` (all repos) | 80% | `pytest --cov=repositories` |
| `core/` (shared logic) | 85% | `pytest --cov=core` |
| `routers/` (route handlers) | 70% | `pytest --cov=routers` |
| `dependencies/` | 75% | `pytest --cov=dependencies` |
| **Overall project** | **75%** | `pytest --cov=app` |

Coverage is enforced in CI. A PR that drops coverage below threshold requires justification and an exemption from the tech lead.

---

## 5. What NOT to Test

The following are explicitly excluded from the test suite:

| Artifact | Reason | Exception |
|---|---|---|
| Auto-generated OpenAPI client code | Generated, not written | Only smoke-test that generated client can call live server |
| Third-party library internals | Not our code | Only test our integration boundary (function calls, not library internals) |
| Alembic migration scripts | Tested once during development | Always review auto-generated migrations manually |
| Graphiti internals | External library, Apache 2.0 | Only test our wrapper (`graphiti-client/`) |
| LLM model behaviour | Non-deterministic by nature | Test extraction prompt output format via evals, not unit tests |
| Configuration defaults | Env var parsing tested in `core/config.py` | No need to test that `os.getenv` works |
| Dashboard frontend (Next.js) | Separate JS test strategy | Only test dashboard API proxy routes from the API layer |

---

## 6. Test Tagging

All tests must carry at least one marker from the following:

```python
@pytest.mark.unit       # Fast, no I/O
@pytest.mark.integration # Real DB/Redis via testcontainers
@pytest.mark.e2e        # Full Docker Compose stack
@pytest.mark.security   # Cross-tenant, auth bypass, rate limiting
@pytest.mark.perf       # Load/performance (Locust or k6)
@pytest.mark.eval       # LLM quality eval against golden datasets
@pytest.mark.slow       # Any test taking > 5s (used to skip in pre-commit)
```

Run selection:

```bash
# Pre-commit (linter only)
ruff check .

# Per-commit CI
pytest -m "unit or integration" -x --timeout=60

# Merge to main
pytest -m "not perf and not eval" --timeout=300

# Nightly
pytest -m "perf" --locust
pytest -m "eval"
```

---

## 7. CI Separation

| Event | Tests Run | Max Time |
|---|---|---|
| Every branch push | `lint` + `unit` + `integration` | < 5 min |
| Merge to `main` | `unit` + `integration` + `e2e` + `security` + `sdk` | < 15 min |
| Tagged commit (`v*`) | All of above + `publish` | < 20 min |
| Nightly (scheduled) | `performance` + `eval` | < 30 min |
| Weekly (scheduled) | Full eval suite + coverage report | < 45 min |

See [06-ci-pipeline.md](./06-ci-pipeline.md) for detailed pipeline configuration.

---

## 8. Test Data Management

- **Static fixtures** live in `tests/fixtures/`: LLM mock responses, JSON schemas, sample conversations
- **Golden datasets** live in `tests/fixtures/datasets/`: versioned JSON Lines files for LLM evals
- **Factories** live in `tests/factories/`: `UserFactory`, `SessionFactory`, `EpisodeFactory`, `FactFactory` — insert into DB with sensible defaults
- **Test seed data** for integration tests is managed via `conftest.py` fixtures, not SQL files
- **Sensitive data** (test API keys, mock secrets) never committed — use `.env.test` with `pydantic-settings`

---

## 9. Failure Handling in Tests

| Scenario | Behaviour |
|---|---|
| Testcontainer fails to start | Test is **skipped** (not failed), logged with container logs |
| Integration test hits real LLM | Fail hard — `MockLLM` must be injected, real client blocked via env guard |
| Flaky test (intermittent) | Tag `@pytest.mark.flaky(reruns=3)` with a comment explaining the flake |
| Slow test (> 5s but not perf) | Tag `@pytest.mark.slow` — excluded from pre-commit, included in CI |
| Coverage drops below threshold | CI fails, report uploaded as artifact |

---

## 10. Tooling

| Tool | Purpose | Config |
|---|---|---|
| `pytest` | Test runner | `pyproject.toml` |
| `pytest-asyncio` | Async test support | `asyncio_mode = "auto"` |
| `pytest-cov` | Coverage measurement | `--cov=app --cov-report=term --cov-report=html` |
| `pytest-xdist` | Parallel execution (unit only) | `-n auto` |
| `testcontainers` | DB containers for integration | `testcontainers==4.*` |
| `locust` | Load generation | `locustfile.py` in `tests/performance/` |
| `bandit` | Python SAST | `bandit -r app/` |
| `httpx` | Async HTTP test client | `ASGITransport` for in-process tests |
| `aresponses` | Mock external HTTP (LLM API) | For unit tests calling external APIs |
| `freezegun` | Time freezing for temporal tests | For bi-temporal fact validation |
| `polyfactory` | Auto-generate Pydantic model instances | For property-based schema tests |
