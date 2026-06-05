# MemGraph — Phase-by-Phase Development Timeline

> **Master Plan** — Synthesised from all specialist reviews: @architect, @senior-dev, @reviewer, @qa-engineer, @junior-mentor, @devops
>
> **Document Status:** Final | **Last Updated:** 2026-06-05 | **Total Duration:** 20 weeks (8 phases)
> **Original SRS estimate:** 14 weeks (6 phases) — recalibrated to **20 weeks** (+43%) based on production-grade rigour.

---

## Executive Summary

| Metric | Original SRS | Revised | Delta |
|--------|-------------|---------|-------|
| Total duration | 14 weeks | **20 weeks** | +6 weeks |
| Phases | 6 | **8** (split Phase 5) | +2 |
| Person-weeks (est.) | ~84 | **~170** | +86 |
| Team size implicit | 2-3 | **3-4** (2 senior, 1 mid, 1 junior) | +1 |
| Testing effort | Phase 5 only | **Continuous, ~25% of each phase** | — |
| Parallelism | Mostly serial | **3-track parallel in most phases** | — |

### Why the +6 Week Delta?

| Factor | Weeks Added | Detail |
|--------|------------|--------|
| Phase 1 was 3x overcommitted | +2 | 6 interdependent features in 2 weeks → 4 weeks |
| Phase 5 was impossible | +1.5 | 80% coverage + load test + security audit + docs in 2 weeks → split into 5a (3 weeks) + 5b (1 week) = 4 weeks (Go SDK and TS SDK removed from scope) |
| Missing NLP eval infrastructure | +1 | Golden datasets, eval suite, prompt iteration cycles not accounted for |
| Production deployment hardening | +1 | Redis Sentinel, pgBouncer, Helm chart, air-gapped, DR drills |
| LLM prompt tuning cycles | +0.5 | Entity/fact extraction prompt iteration is unpredictable (30-50% overhead) |

---

## Phase Dependency Graph

```
Phase 0 ──► Phase 1 ──► Phase 2 ──► Phase 3 ──► Phase 4 ──► Phase 5a ──► Phase 5b
(2 wks)    (4 wks)     (3 wks)     (3 wks)     (4 wks)     (3 wks)      (1 wk)
Foundation  Core Mem    Full Parity NLP Enrich  Dash+Infra  Hardening    Release

         Parallel Tracks Within Each Phase:
         ┌────────────────────────────────────────────────────┐
         │ Track A: API/Core (senior carries critical path)   │
         │ Track B: NLP/Workers (second senior, 1-2wk lag)    │
         │ Track C: DevOps/Infra (mostly independent)          │
         │ Junior rotates between tracks for well-defined tasks│
         └────────────────────────────────────────────────────┘
```

**Hard dependency rule:** Phase N cannot start until Phase N-1 exit criteria are met. No exceptions.

---

## Phase 0 — Foundation (Weeks 1–2)

**Theme:** *"Docker Compose up works on a laptop, auth works, data model is solid."*

> **Components marked 🔒 are proprietary (core IP).**

### Team
| Role | Person | Allocation |
|------|--------|------------|
| Senior backend (arch) | Track A | 100% |
| Mid backend | Track B | 100% |
| DevOps | Track C | 20% (consulting) |

### Tasks by Track

**Track A — Core Infrastructure (Engineer A: Senior)**

| Week | Task | Est. Days | Depends On |
|------|------|-----------|------------|
| W1 | Monorepo scaffold: `services/api`, `services/worker`, `packages/core`, `packages/graphiti-client`, `tests/`, `docs/`, `infra/` | 1 | — |
| W1 | 🔒 `core/config.py` — pydantic-settings with all env vars, validation, defaults | 1 | 0.1 |
| W1 | 🔒 `core/db.py` — async engine + session factory, `pool_pre_ping=True`, `pool_size=20`, `expire_on_commit=False` | 1 | 0.2 |
| W1 | PostgreSQL DDL: all 12 tables + 18 indexes + CHECK constraints + RLS migration | 3 | 0.3 |
| W1–2 | Alembic baseline migration + parameterized `EMBEDDING_DIM` | 2 | 0.4 |
| W2 | FastAPI `create_app()` factory: lifespan, middleware chain, router registration, `/health` + `/ready` | 2 | 0.3 |
| W2 | Global exception handler: RFC 7807 Problem Details, error code catalogue (17 codes) | 1 | 0.6 |

**Track B — Auth & Security (Engineer B: Mid)**

| Week | Task | Est. Days | Depends On |
|------|------|-----------|------------|
| W1 | API key auth: `api_keys` model, SHA-256 hashing (16-byte salt), create/validate/rotate/revoke | 3 | 0.3 |
| W1–2 | JWT auth for dashboard: access token (15min), refresh token (7d, rotation on use) | 2 | 0.5 |
| W2 | Multi-tenant RLS: `TenantSessionMiddleware`, `set_config('app.org_id')` on every request | 2 | 0.6 |
| W2 | Rate limiting: per-IP (10 failed auth/min) + per-key (configurable), Redis sliding window | 1 | 0.7 |
| W2 | 🔒 `core/exceptions.py` — AppError hierarchy, all typed exceptions | 1 | 0.6 |
| W2 | 🔒 `core/logging.py` — structlog config, JSON formatter, PII redaction processor | 1 | 0.6 |

**Track C — Graph DB & BYOK LLM (shared)**

| Week | Task | Est. Days | Depends On |
|------|------|-----------|------------|
| W2 | Graphiti integration: version pinning (`graphiti-core==0.29.1`), async wrapper, circuit breaker scaffold | 3 | 0.3 |
| W2 | **BYOK `LLMBackend` abstraction**: `LLMBackend` ABC with `OllamaBackend`, `OpenAIBackend`, `AzureBackend`, `AnthropicBackend` implementations. `EmbeddingClient` as part of BYOK stack — first-run auto-detects Ollama (local, zero-config) or prompts user to configure any provider. Dimension validation, batch embedding. **No default API key shipped.** | 3 | 0.3 |
| W2 | Docker Compose dev: api, postgres+pgvector, falkordb, redis, alloy (optional ollama) | 1 | 0.1 |

### Phase 0 — Junior Ownership Map

| Task | Difficulty | Ownership | Why |
|------|-----------|-----------|-----|
| `core/config.py` — pydantic-settings | Low | Junior solo | Well-documented pattern, no business logic |
| `TimestampMixin` in models/ | Low | Junior solo | Mechanical, teaches declarative base patterns |
| `routers/health.py` — health + readiness | Low | Junior solo | Pure FastAPI, teaches lifespan DI |
| Dev Docker Compose | Low-Med | Junior solo | `docker-compose.yml` with health checks |
| Alembic baseline migration | Med | Junior+senior pair | Parameterized DDL — important to get right but docs exist |
| `middleware/request_id.py` | Med | Junior+senior pair | First middleware, teaches `request.state` pattern |

### Exit Criteria (ALL must pass)

| # | Criterion | Verification |
|---|-----------|-------------|
| **G0.1** | `docker compose up` boots all 5 services with green health checks | `docker compose ps` shows all healthy |
| **G0.2** | Alembic baseline: `upgrade head` creates 12 tables + 18+ indexes; `downgrade -1` drops to empty | Integration test: `test_migrations.py` (3 tests) |
| **G0.3** | API key create → hash → validate → rotate → revoke works end-to-end | Integration test: `test_auth.py` (5 tests) |
| **G0.4** | RLS: 3-org × 2-user matrix — same-org returns 200, cross-org returns 404 | Cross-tenant test: `test_cross_tenant.py` (36 tests) |
| **G0.5** | BYOK: Ollama auto-detected on fresh install; user prompted if no local LLM available; all 4 backends configurable via env. Dimension mismatch raises `ConfigurationError`. | Unit test + integration |
| **G0.6** | No `VECTOR(1536)` literal in any DDL — dimension is config-driven | `grep -r "VECTOR(1536)" models/ schemas/` = empty |
| **G0.7** | Every domain has: `services/{domain}_service.py`, `repositories/{domain}_repository.py`. No router imports model directly | Static analysis: grep patterns |
| **G0.8** | structlog produces valid JSON. Every log includes `service`, `environment`, `trace_id` | Manual: check log output |

### Risk Gate for Phase 1

> **Single biggest risk:** RLS policy correctness under concurrent pooled connections.
> `set_config('app.org_id')` is transaction-local — it clears on pool return. A stale connection could serve cross-tenant data.
> **Mitigation:** `TenantSessionMiddleware` sets `app.org_id` BEFORE every route handler runs. Integration test proves zero cross-tenant leaks under 100 concurrent connections.
> **Must pass before Phase 1 starts.**

### Teaching Sessions (Before Phase 0)

| Day | Session | Duration | Led By |
|-----|---------|----------|--------|
| Day 1 | System overview & architecture walkthrough | 90 min | Tech lead |
| Day 1 | Dev environment setup | 60 min | Senior dev |
| Day 2 | Coding standards workshop (DDD layering, PR checklist, async rules) | 60 min | Tech lead |
| Day 2 | SQLAlchemy 2.x async deep-dive | 90 min | Senior dev |
| Day 3 | FastAPI app factory + middleware chain | 60 min | Senior dev |
| Day 4 | Auth & multi-tenancy design | 90 min | Tech lead |

---

## Phase 1 — Core Memory (Weeks 3–6)

**Theme:** *"API + worker + Postgres integration tests pass in CI. Context assembly works under 300ms."*

> **Components marked 🔒 are proprietary (core IP).**

> **Original estimate:** 2 weeks → **Corrected: 4 weeks.** The SRS crammed 6 interdependent features (user CRUD, session CRUD, message ingestion, entity extraction, embedding, context assembly) into 10 working days. Each feature requires router → schema → service → repository → worker task → tests. Entity extraction alone needs prompt engineering (3-5 iterations), LLM integration, retry/backoff, output parsing, and eval against golden dataset.

### Team
| Role | Person | Allocation |
|------|--------|------------|
| Senior backend | Track A (critical path) | 100% |
| Mid backend | Track B | 100% |
| Junior | Rotating (CRUD + tests) | 50% |

### Tasks by Track

**Track A — API & Ingestion (Engineer A: Senior)**

| Week | Task | Est. Days | Depends On |
|------|------|-----------|------------|
| W3 | 🔒 User CRUD endpoints (POST/GET/PATCH/DELETE/LIST) — full DDD stack | 2 | P0 exit |
| W3 | 🔒 Session CRUD endpoints — full DDD stack, `sequence_number` ordering | 2 | 1.1 |
| W4 | 🔒 Message ingestion (`POST /memory`): `MemoryService.ingest()`, batch episode insert, ARQ enqueue | 4 | 1.1, 1.2 |
| W4 | HTTP-level idempotency: `Idempotency-Key` header, Redis cache (48h TTL), content-hash dedup | 3 | 1.3 |
| W4 | 🔒 `sync_to_graph` worker: populate Graphiti episodic layer ASYNCHRONOUSLY (fixes dual-write) | 2 | 1.3 |
| W5–6 | 🔒 **Context assembly** (`GET /context`): BFS (depth 2) + vector + BM25 + RRF + cache-aside | 6 | 1.4, 1.5 |
| W6 | 🔒 Caching strategy: Redis 30s TTL, invalidation on ingest, cache stampede prevention, hit-rate metrics | 2 | 1.6 |

**Track B — Workers & NLP (Engineer B: Mid)**

| Week | Task | Est. Days | Depends On |
|------|------|-----------|------------|
| W3 | 🔒 ARQ worker process: Redis connection, worker entrypoint, health checks, graceful shutdown | 3 | 0.8 |
| W4 | 🔒 Worker-level idempotency: `episodes.enrichment_status` bitmask, `SELECT ... FOR UPDATE` | 2 | 1.3 |
| W5 | 🔒 **Entity extraction worker**: prompt (`extract_entities_v1.jinja2`), JSON mode parsing, retry/backoff | 5 | 1.3 |
| W5 | 🔒 **Embedding worker**: batch embedding (100/batch), pgvector update, dim validation | 3 | 1.4 |
| W6 | 🔒 **Fact extraction worker**: zero-shot prompt, `facts` persistence, confidence scoring | 4 | 1.3 |
| W6 | 🔒 Worker task definitions: all 5 core tasks with specs (input/output schema, timeout, retry policy) | 2 | 1.4 |

**Parallel within Track B:**

| Junior (rotating) | Est. Days |
|-------------------|-----------|
| Missing FK index audit + migration (14 indexes) | 1 |
| Missing tables: `extraction_schemas`, `refresh_tokens`, `audit_log`, `llm_usage` | 2 |
| Full-text search indexes: GIN on `facts.content` (was missing!), GIN on `episodes.content` | 1 |
| Test writing for user/session CRUD | 2 |

### Phase 1 — Junior Ownership Map

| Task | Difficulty | Ownership |
|------|-----------|-----------|
| `schemas/memory.py` — Pydantic models for ingestion | Low | Junior solo |
| `routers/users.py` — User CRUD endpoints | Low | Junior solo |
| `routers/sessions.py` — Session CRUD endpoints | Low | Junior solo |
| `repositories/user_repository.py` | Low | Junior solo |
| `repositories/session_repository.py` | Low | Junior solo |
| FK index audit + migration | Low | Junior solo |
| Missing tables creation | Med | Junior+senior pair |
| `services/user_service.py` | Med | Junior+senior pair |
| Worker: `embed_episode` | Med | Junior+senior pair |

### Exit Criteria (ALL must pass)

| # | Criterion | Verification |
|---|-----------|-------------|
| **G1.1** | `POST /memory` with 10-turn conversation returns 202 within 200ms | Integration test |
| **G1.2** | Entity extraction worker completes within 30s, entity nodes visible in FalkorDB | Integration test |
| **G1.3** | Episodes have `embedding` populated (non-NULL) after worker completes | DB query |
| **G1.4** | `GET /context?query="python"` returns assembled text with relevant facts, p99 cold ≤1500ms, p99 warm ≤300ms | Load test (10 concurrent users, 500 facts, 100 episodes) |
| **G1.5** | All 8 user/session CRUD endpoints pass cross-tenant tests | Test matrix 100% |
| **G1.6** | `enrichment_status` bitmask correctly tracks progress | Unit test (set bits, verify & operations) |
| **G1.7** | `DELETE /users/{user_id}/memory` — memory wipe endpoint exists and works | Integration test |
| **G1.8** | Field-level error details on all 422 responses | Integration test |
| **G1.9** | Idempotency: same `Idempotency-Key` → same 202, different payload → 422 | Integration test |
| **G1.10** | Unit test coverage ≥ 50% on `services/`, `repositories/` | `pytest --cov` |

### Risk Gate for Phase 2

> **Single biggest risk:** Context assembly latency budget.
> Target: p99 ≤300ms warm, ≤1500ms cold. Requires Redis cache hit rate >80%, tuned pgvector `probes`, BFS depth limited to 2 hops, RRF merge optimised.
> **Exit condition proved by load test** — not by optimistic estimation.

### Teaching Sessions (Before Phase 1)

| Day | Session | Duration | Led By |
|-----|---------|----------|--------|
| W3 D1 | ARQ worker system walkthrough | 60 min | Senior dev |
| W3 D1 | DDD canonical pattern: live code `POST /memory` start-to-finish | 60 min | Tech lead |
| W3 D2 | Hybrid retrieval architecture + latency budget analysis | 90 min | Tech lead |
| W3 D3 | Async enrichment pipeline (ingestion → worker dispatch → enrichment → embedding) | 60 min | Senior dev |
| W3 D4 | Graphiti TKG engine: episodic vs semantic vs temporal layers | 60 min | Tech lead |
| W4 D1 | Prompt engineering for extraction | 60 min | Tech lead |

---

## Phase 2 — Full Feature Parity (Weeks 7–9)

**Theme:** *"SDK published to PyPI, MCP server works in Claude Desktop, graph queries performant, community summaries generated."*

> **Components marked 🔒 are proprietary (core IP). Items marked 📦 are open-source.**

> **Original estimate:** 2 weeks → **Corrected: 3 weeks.** Python SDK with sync/async duality + full test suite + PyPI publishing is 1.5 weeks. MCP server with 8 tools + auth + stdio/SSE is another 1+ weeks. These cannot run in parallel with graph query endpoints and community summarisation — they all depend on the same stable API surface. Scope reduced by removing TypeScript SDK and Go SDK efforts from this phase.

### Team
| Role | Person | Allocation |
|------|--------|------------|
| Senior backend | Track A (SDK + API) | 100% |
| Senior backend | Track B (MCP + Graph) | 100% |
| DevOps | Track C (infra) | 50% |

### Tasks by Track

**Track A — API & SDK (Engineer A: Senior)**

| Week | Task | Est. Days | Depends On |
|------|------|-----------|------------|
| W7–8 | 📦 **Python SDK** (`memgraph`): sync/async duality, all 5 domains (memory, facts, graph, users, sessions), `PaginatedAsyncIterator`, typed errors, PyPI CI pipeline | 8 | Phase 1 stable API |
| W9 | 📦 SDK integration tests (against running API in CI) | 2 | 2.1 |
| W9 | 🔒 Business data ingestion: `POST /facts` with batch triples (max 500), validation, low-queue ARQ task | 2 | 1.1 |
| W9 | 🔒 Hybrid search endpoint: `GET /search?query=&types=` with vector + BM25 + graph + RRF | 2 | 1.6 |

**Track B — Graph & MCP (Engineer B: Senior)**

| Week | Task | Est. Days | Depends On |
|------|------|-----------|------------|
| W7 | 🔒 Graph query endpoints: `GET /nodes`, `GET /nodes/{id}`, `GET /edges`, `DELETE /nodes/{id}`, cursor pagination | 3 | 0.9 (Graphiti) |
| W7 | 📦 Graph client abstraction: `GraphBackend` interface, FalkorDB implementation, `org_id` enforcement | 2 | 2.4 |
| W7–8 | 📦 **MCP server**: stdio + SSE transports, 8 tools (add_memory, get_context, search_memory, add_fact, list_facts, get_user_graph, create_user, list_sessions), auth, Claude Desktop config | 4 | 2.1 |
| W8–9 | 🔒 **Community summarisation**: Louvain/Label Propagation algorithm, LLM summary generation, nightly schedule | 4 | 2.4 |
| W9 | 🔒 Full-text search: `pg_trgm` GIN indexes, combined with pgvector + graph in RRF | 2 | 2.6 |

**Track C — DevOps (Engineer C: DevOps)**

| Week | Task | Est. Days | Depends On |
|------|------|-----------|------------|
| W7 | CI/CD: e2e tests stage, security tests stage, SDK tests stage | 2 | P0 CI |
| W7 | Prometheus metrics: `/metrics` endpoint, worker metrics, Alloy → Mimir pipeline | 2 | 0.8 |
| W8 | Production Docker Compose: pgBouncer (pool_size=25), Redis Sentinel (master+2replicas), Traefik with TLS | 3 | 0.10 |
| W8–9 | Grafana dashboards: 6 provisioning panels (API RED, context latency, queue depth, token usage, graph growth, service health) | 2 | 2.8 |
| W9 | Helm chart scaffold: `Chart.yaml`, `values.yaml`, deployment templates, services, PVCs | 3 | 2.9 |

### Exit Criteria (ALL must pass)

| # | Criterion | Verification |
|---|-----------|-------------|
| **G2.1** | `pip install memgraph` → `client.memory.add()` returns typed response | Integration test |
| **G2.2** | MCP server starts with stdio + SSE. All 8 tools respond correctly | Integration test + Claude Desktop manual |
| **G2.3** | Graph query: `GET /graph/nodes` with 15k entity nodes returns ≤500ms p99 | Load test |
| **G2.4** | Community summary generated for 5-entity cluster, `CommunityNode` created in FalkorDB | Integration test |
| **G2.5** | Business data: POST 500 fact triples in < 5s, stored in facts + graph | Integration test |
| **G2.6** | Full-text search returns BM25 results merged with vector + graph in RRF | Integration test |
| **G2.7** | Production Docker Compose: all services healthy, pgBouncer connected, Sentinel failover <10s | Manual smoke test |
| **G2.8** | Grafana dashboard shows API latency, queue depth, token usage | Visual verification |
| **G2.9** | Unit test coverage ≥ 70% on `services/`, `repositories/` | `pytest --cov` |

### Risk Gate for Phase 3

> **Single biggest risk:** Graph query latency with user graphs >10k nodes.
> BFS depth=2 on a 10k-node graph can return thousands of nodes if connectivity is dense. Response payload alone could be >1MB.
> **Exit criterion:** `GET /users/{user_id}/graph/nodes` with 15k entity nodes returns ≤500ms p99. BFS with depth=2 on densely connected 10k-node graph returns ≤100 edges.

### Teaching Sessions (Before Phase 2)

| Day | Session | Duration | Led By |
|-----|---------|----------|--------|
| W7 D1 | SDK design patterns (sync/async, retry, error model) | 90 min | Tech lead |
| W7 D1 | OpenAPI-first SDK generation | 60 min | Senior dev |
| W7 D2 | MCP protocol overview (JSON-RPC over stdio/SSE) | 60 min | Tech lead |
| W7 D3 | Community detection algorithms (Louvain vs Label Propagation) | 60 min | Tech lead |
| W8 D1 | Production Docker Compose: pgBouncer, Sentinel, Traefik | 60 min | DevOps |

---

## Phase 3 — NLP Enrichment (Weeks 10–12)

**Theme:** *"All 17 NLP requirements have quantified acceptance criteria. Golden datasets exist. LLM evals block the pipeline."*

> **Components marked 🔒 are proprietary (core IP).**

> **Original estimate:** 2 weeks → **Corrected: 3 weeks.** This is the highest-risk phase. 17 NLP requirements with no acceptance criteria in the original SRS. Each feature requires: prompt engineering (3-5 iterations per domain), golden dataset creation, eval suite integration, and iteration until accuracy thresholds are met. Dialog classification alone (intent + emotion, configurable labels per org) is 1 week minimum.

### Team
| Role | Person | Allocation |
|------|--------|------------|
| Senior (NLP/LLM) | Track A (prompts + workers) | 100% |
| Mid backend | Track B (endpoints + schemas) | 100% |

### Tasks by Track

**Track A — Prompt Engineering & Workers (Engineer A: Senior NLP)**

| Week | Task | Est. Days | Depends On |
|------|------|-----------|------------|
| W10 | 🔒 Dialog classification pipeline: prompt (`classify_dialog_v1.jinja2`), LLM structured output, configurable labels per org | 4 | 1.6 (entity pattern) |
| W10 | 🔒 Classification eval: golden dataset (200 labeled turns), accuracy ≥85%, eval suite in CI | 2 | 3.1 |
| W11 | 🔒 Structured extraction worker: org-defined JSON Schema → LLM call → jsonschema validation | 4 | 3.1 |
| W11 | 🔒 Structured extraction eval: 10 schema variations, 100% schema compliance | 2 | 3.2 |
| W12 | 🔒 Custom entity ontologies: per-org entity types in extraction prompts, prompt injection guardrails | 3 | 1.6 |
| W12 | 🔒 Entity merge dedup worker: weekly scheduled task, `LOWER(name)+org_id` dedup, audit trail | 3 | 3.3 |

**Track B — Endpoints & Infrastructure (Engineer B: Mid)**

| Week | Task | Est. Days | Depends On |
|------|------|-----------|------------|
| W10 | Classification endpoints: `GET /users/{id}/sessions/{sid}/classifications` | 2 | 3.1 |
| W10 | Schema CRUD API: `POST/GET/PUT/DELETE /v1/admin/schemas` | 3 | — |
| W11 | Structured extraction query endpoint: `GET /users/{id}/sessions/{sid}/extract` | 2 | 3.2 |
| W11–12 | **LLM cost control**: per-org daily token budget, enrichment depth levels (none/basic/full), `llm_usage` table, hard cutoff (429) | 4 | 1.9 (llm_usage table) |
| W12 | PII detection pre-processor (regex + spaCy NER, configurable per org) | 2 | 1.3 |
| W12 | Cost projection dashboard: Grafana panel per-org token consumption, projected overrun date | 2 | 3.5 |

### Exit Criteria (ALL must pass)

| # | Criterion | Verification |
|---|-----------|-------------|
| **G3.1** | Dialog classification accuracy ≥85% on golden dataset (200 labeled turns) | `tests/evals/test_classification.py` |
| **G3.2** | Structured extraction: 10 schema variations return valid JSON matching schema | Integration test |
| **G3.3** | Custom ontology: entity extraction F1 ≥80% with org-defined types | Eval against annotated dataset |
| **G3.4** | Entity merge detects + merges ≥90% of known duplicates | Eval with seeded duplicates |
| **G3.5** | Per-org cost controls: exceed daily budget → 429, enrichment depth levels enforced | Integration test |
| **G3.6** | PII redaction: known PII patterns stripped before LLM call, configurable per org | Integration test |
| **G3.7** | Eval suite runs on every merge to `main`, regression > 2% blocks the pipeline | CI verified |
| **G3.8** | Unit test coverage ≥ 75% across all packages | `pytest --cov` |

### Risk Gate for Phase 4

> **Single biggest risk:** LLM cost blowout.
> Each ingestion now triggers: entity extraction + fact extraction + dialog classification (per turn) + structured extraction (per session). For an org with 10k conversations/day = 50k+ LLM calls/day.
> **Exit criterion:** Cost controls operational in production — enrichment depth levels, daily token budget with hard 429 cutoff, BYOK with Ollama auto-detection as zero-config default, cost per step tracked in `llm_usage` + Grafana dashboard.

### Teaching Sessions (Before Phase 3)

| Day | Session | Duration | Led By |
|-----|---------|----------|--------|
| W10 D1 | Dialog classification + structured extraction architecture | 60 min | Tech lead |
| W10 D1 | Eval methodology for NLP (golden datasets, regression testing, thresholds) | 60 min | Tech lead |
| W10 D2 | PII detection & guardrails | 45 min | Security lead |
| W10 D3 | LLM cost control patterns | 45 min | Tech lead |

---

## Phase 4 — Dashboard & Production Infra (Weeks 13–16)

**Theme:** *"Helm chart deploys on 3-node cluster. Production environment is live. Dashboard usable by non-engineer."*

> **Components marked 🔒 are proprietary (core IP).**

> **Original estimate:** 3 weeks → **Corrected: 4 weeks.** Dashboard alone (Next.js with auth, tenant mgmt, graph explorer, analytics) is 2+ weeks for a quality result. Helm chart with HPA, PDB, NetworkPolicies, External Secrets is another full week. Production Docker Compose with Redis Sentinel + pgBouncer needs careful testing.

### Team
| Role | Person | Allocation |
|------|--------|------------|
| Senior full-stack | Track A (dashboard) | 100% |
| Senior infra/backend | Track B (production infra) | 100% |
| DevOps | Track C (K8s + CI/CD) | 100% |

### Tasks by Track

**Track A — Dashboard (Engineer A: Senior Full-Stack)**

| Week | Task | Est. Days | Depends On |
|------|------|-----------|------------|
| W13 | Next.js 14 setup: App Router, auth (JWT + refresh), auto-generated API client from OpenAPI, shadcn/ui + Tailwind | 3 | P0 (JWT) |
| W13–14 | Tenant management UI: org CRUD, API key generate/revoke (show-once modal), quota configuration | 4 | 4.1 |
| W14–15 | User graph explorer: Cytoscape.js integration, entity visualisation, search, zoom/pan | 5 | 2.4 |
| W15–16 | Analytics panels: usage charts from Mimir queries, API stats, token usage, worker queue depth | 4 | 2.8 (Grafana) |

**Track B — Production Infra (Engineer B: Senior)**

| Week | Task | Est. Days | Depends On |
|------|------|-----------|------------|
| W14 | Production Docker Compose finalisation: pgBouncer tuning, Sentinel failover test, Traefik TLS | 3 | 2.7 |
| W15–16 | **Kubernetes Helm chart**: Deployment + HPA (API CPU>70%, Worker queue depth), PDB (minAvailable=1), PV (PG 100Gi, FalkorDB 50Gi, Redis 10Gi), NetworkPolicies (deny-all + allow inter-service), External Secrets Operator | 6 | 4.3 |
| W16 | `CORS_ORIGINS` env var, CSRF protection for dashboard, rate limit on login | 1 | 4.1 |

**Track C — DevOps (Engineer C: DevOps)**

| Week | Task | Est. Days | Depends On |
|------|------|-----------|------------|
| W13–14 | CI/CD: full pipeline (lint → unit → integration → build → e2e → security → sdk → publish) | 3 | 2.7 |
| W14 | Prometheus adapter for worker HPA (custom metric exposing queue depth) | 2 | 2.8 |
| W15 | Grafana dashboards auto-provisioned via JSON + datasource YAML | 2 | 2.8 |
| W16 | OpenTelemetry tracing: HTTP → ARQ → LLM → Graphiti, Tempo ingestion, log↔trace correlation | 2 | 2.8 |
| W16 | Alert rules: 8 rules (error rate >1%, p99 latency >2s, queue depth >1000, failed task rate >5%) | 1 | 4.7 |

### Exit Criteria (ALL must pass)

| # | Criterion | Verification |
|---|-----------|-------------|
| **G4.1** | Dashboard loads in browser, JWT auth works, tenant CRUD functional | Manual E2E |
| **G4.2** | Graph explorer renders 50+ nodes with edges, search works | Manual test with seed data |
| **G4.3** | Helm chart: `helm install` on 3-node cluster, all pods healthy, HPA scales API 2→4 under load | Load test |
| **G4.4** | Production Compose: Redis Sentinel failover <10s, pgBouncer pool stable at 500 req/s | Smoke test |
| **G4.5** | Grafana dashboards auto-provisioned, all 6 panels render with data | Visual verification |
| **G4.6** | Unit test coverage ≥ 78% on all backend packages | `pytest --cov` |

### Risk Gate for Phase 5a

> **Single biggest risk:** Helm chart deployments on a real cluster.
> **Mitigation:** Deploy on 3-node staging cluster for 48h before Phase 5a starts.
> **Exit criterion:** `helm install` succeeds. `helm upgrade` with zero-downtime migration works. `helm rollback` restores within 60s. HPA scales under Locust load.

### Teaching Sessions (Before Phase 4)

| Day | Session | Duration | Led By |
|-----|---------|----------|--------|
| W13 D1 | Next.js 14 App Router + auth flow | 60 min | Senior full-stack |
| W13 D1 | shadcn/ui + Tailwind conventions | 30 min | Senior full-stack |
| W13 D2 | Auto-generated API client from OpenAPI | 45 min | Senior dev |
| W14 D1 | Kubernetes Helm chart basics | 90 min | DevOps |
| W14 D2 | Grafana dashboard as code | 45 min | DevOps |

---

## Phase 5a — Hardening: Tests & Observability (Weeks 17–19)

**Theme:** *"Everything tested, monitored, and secure. Load test passes at 500 req/s."*

> **Components marked 🔒 are proprietary (core IP).**

> **Original estimate:** Phase 5 was 2 weeks total → **Now split: 5a (3 weeks) + 5b (1 week) = 4 weeks.** The original jammed 80% test coverage, integration suite, load test, security audit, Go SDK, and release docs into 2 weeks — impossible. Phase 5a focuses on test coverage, load testing, security audit, circuit breakers, and PII redaction.

### Team
| Role | Person | Allocation |
|------|--------|------------|
| Senior (test/security) | Track A (tests + security) | 100% |
| Senior (backend) | Track B (circuit breakers + orchestrator + PII) | 100% |
| DevOps | Track C (load test infra + monitoring) | 50% |

### Tasks by Track

**Track A — Tests & Security (Engineer A: Senior)**

| Week | Task | Est. Days | Depends On |
|------|------|-----------|------------|
| W17 | Unit test coverage push: identify <80% packages, add service tests, edge case coverage | 5 | All phases |
| W17 | Integration test suite: P0 + P1 endpoints, testcontainers (PG + Redis + FalkorDB), mock LLM | 5 | 5.1 |
| W18 | **Cross-tenant test matrix**: 3 tenants × 6 resources × 3 access methods = 324 parameterized tests | 2 | 5.1 |
| W18 | Security audit: SQL injection, auth bypass, cross-tenant, dependency scan (pip-audit), SAST (bandit + semgrep) | 4 | 5.2 |
| W19 | Security findings remediation (blocker: fix all critical/high) | 2 | 5.3 |

**Track B — Production Hardening (Engineer B: Senior)**

| Week | Task | Est. Days | Depends On |
|------|------|-----------|------------|
| W17 | 🔒 **Circuit breakers**: LLM API, embedding API, FalkorDB, Redis — `pybreaker` with per-org isolation | 3 | 0.8 |
| W17 | 🔒 Retry audit: every external call has `timeout + retry + circuit breaker + fallback` | 2 | 5.5 |
| W18 | 🔒 **Orchestrator migration**: event-driven → single orchestrator for enrichment pipeline | 3 | 1.8 |
| W18 | 🔒 Graceful degradation: context assembly without graph, ingestion without enrichment | 2 | 5.6 |
| W19 | 🔒 PII redaction pipeline: pre-LLM regex + spaCy NER, configurable per org, blocking mode (422) | 3 | 5.7 |
| W19 | 🔒 Rate limiting rollout: per-key config via admin API, `X-RateLimit-*` headers, 429 body | 2 | 0.8 |

**Track C — Load Test & Monitoring (Engineer C: DevOps)**

| Week | Task | Est. Days | Depends On |
|------|------|-----------|------------|
| W17–18 | **Load test**: 500 req/s sustained × 5 min, hardware = 4 CPU/8GB API + 2 CPU/4GB worker + separate load generator | 4 | Phase 4 infra |
| W18 | Performance bottleneck diagnosis + fix (if load test fails) | 3 | 5.10 |
| W19 | Performance benchmark suite: repeatable, CI-scheduled, regression detection | 2 | 5.11 |
| W19 | SLO dashboards: 99.9% uptime, p99 <300ms, error budget, burn rate alerts | 2 | 4.7 |
| W19 | On-call runbooks: 5 runbooks (API outage, DB failover, worker backlog, LLM outage, security incident) | 2 | — |

### Exit Criteria (ALL must pass)

| # | Criterion | Verification |
|---|-----------|-------------|
| **G5a.1** | Unit test coverage ≥ 80% on all service packages | `pytest --cov` |
| **G5a.2** | Integration tests for ALL P0 endpoints (17 endpoints × happy + error + auth) | CI pipeline |
| **G5a.3** | Cross-tenant matrix: 324/324 tests passing, < 30s runtime | CI pipeline |
| **G5a.4** | Load test: 500 req/s sustained for 5 min, p99 context < 300ms, 0% errors | k6 report |
| **G5a.5** | Security audit: 0 critical, 0 high findings | Report signed off |
| **G5a.6** | Circuit breakers: provider down → circuit opens → fallback works → circuit closes on health | Integration test |
| **G5a.7** | Orchestrator migration: `USE_ORCHESTRATOR=True` in staging for 48h, no regression | Staging verification |
| **G5a.8** | PII redaction: golden PII dataset → 100% redacted before LLM call | Integration test |
| **G5a.9** | SLO dashboards operational, burn rate alerts configured | Visual verification |

### Risk Gate for Phase 5b

> **Single biggest risk:** Load test failure under 500 req/s.
> If this fails, do NOT proceed to Phase 5b. Diagnose with `py-spy` + `EXPLAIN ANALYZE` and fix.
> **Exit criterion:** 500 req/s × 5 min, all pass criteria met, stable RSS for 10 min post-load.

---

## Phase 5b — Release Readiness (Week 20)

**Theme:** *"Air-gapped works. All docs finalised. v1.0.0 released."*

> **Components marked 🔒 are proprietary (core IP).**

> **This is the buffer phase.** Items deferred from earlier phases get completed here. If earlier phases ran over, this phase gets compressed — but NEVER cut the air-gapped verification.

### Team
| Role | Person | Allocation |
|------|--------|------------|
| Senior backend | Track A (release infrastructure) | 100% |
| Mid backend | Track B (migration + docs) | 100% |

### Tasks by Track

**Track A — Release Infrastructure (Engineer A: Senior)**

| Week | Task | Est. Days | Depends On |
|------|------|-----------|------------|
| W20 | Final air-gapped verification: full deploy from tarball + local mirrors, no internet access, ingest→enrich→retrieve passes | 1 | Phase 5a infra |
| W20 | Release artifacts: CHANGELOG (keepachangelog format), CONTRIBUTING.md (PR workflow + standards), LICENSE (AGPL v3 — dual-licensed: AGPL v3 for open-source use, commercial license available for SaaS providers), README.md with quickstart | 2 | — |
| W20 | Final security sweep: full SAST + DAST + dependency scan + secrets scan | 1 | 5a.3 |
| W20 | Release v1.0.0: tag, CI publish to PyPI + Docker Hub + GitHub | 1 | 5b.3 |

**Track B — Migration & Documentation (Engineer B: Mid)**

| Week | Task | Est. Days | Depends On |
|------|------|-----------|------------|
| W20 | 🔒 **Bi-temporal upgrade**: `facts_history` table, `FOR SYSTEM_TIME AS OF` queries, migration runbook for 500K+ facts | 2 | 1.8 (facts) |
| W20 | 🔒 Admin dashboard: ARQ job monitor (queue depth, success/failure count, DLQ inspection) | 2 | 5.6 |
| W20 | Documentation finalisation: all implementation docs audited against code | 1 | — |

### Exit Criteria (ALL must pass)

| # | Criterion | Verification |
|---|-----------|-------------|
| **G5b.1** | Air-gapped: full deploy from tarball + local mirrors, no internet access, ingest→enrich→retrieve passes | VM test (no network) |
| **G5b.2** | Bi-temporal: `GET /facts?valid_at=2026-01-15` returns correct historical state | Eval against known versions |
| **G5b.3** | Dashboard job monitor: shows queue depth, success/failure rates, DLQ | Visual + integration test |
| **G5b.4** | All release artifacts published: PyPI, Docker Hub, GitHub | Verified |
| **G5b.5** | Overall unit test coverage ≥ 80% | `pytest --cov` |
| **G5b.6** | All implementation docs match code (final audit) | `doc-audit` CI stage |
| **G5b.7** | v1.0.0 tagged and released | GitHub release page |

### Release Go/No-Go

**ALL of these must be true to release v1.0.0:**

1. All quality gates across all phases passing
2. Security audit: 0 critical, 0 high findings
3. Load test: 500 req/s × 5 min, all pass criteria met
4. Cross-tenant matrix: 324/324 tests passing
5. Unit test coverage ≥ 80%
6. GDPR compliance verified (delete cascade + data portability)
7. Encryption at rest verified
8. Air-gapped deployment verified
9. All implementation docs audited against code
10. Tech lead + Security engineer sign-off

---

## Summary: All Phases at a Glance

| Phase | Weeks | Engineers | Focus | Key Risk | Junior % |
|-------|-------|-----------|-------|----------|----------|
| P0 — Foundation | 1–2 | 2 | Monorepo, auth, RLS, Docker Compose | RLS correctness under pooled connections | 30% |
| P1 — Core Memory | 3–6 | 2.5 | Ingestion, enrichment, context assembly | Context latency budget (p99 ≤ 300ms) | 20% |
| P2 — Full Parity | 7–9 | 2.5 | SDK, MCP, graph queries, communities | Graph query latency at 10k+ nodes | 10% |
| P3 — NLP Enrichment | 10–12 | 2 | Classification, extraction, cost control | LLM cost blowout | 10% |
| P4 — Dashboard & Infra | 13–16 | 3 | Dashboard, Helm, production infra | Helm chart on real cluster | 10% |
| P5a — Hardening | 17–19 | 2.5 | Tests, load test, security, circuit breakers | Load test failure at 500 req/s | 5% |
| P5b — Release | 20 | 2 | Release infra, air-gapped, docs, bi-temporal | Air-gapped verification | 5% |

---

## Resource Ramp

```
Weeks:   1  2  3  4  5  6  7  8  9  10 11 12 13 14 15 16 17 18 19 20
Phase:   P0    P1          P2      P3       P4             P5a    P5b
         │     │           │       │        │              │      │

Engineers:
Senior A  ████████████████████████████████████████████████████████████████
Senior B  ████████████████████████████████████████████████████████████
Mid       ████████████████████████████████████████████████████████████████
Junior    ████████  ██████     ████    ████          ████████  ██████████
DevOps          ██        ████████████                         ██████████

         ██ = Full-time  ██ = Part-time
```

---

## Technical Debt Log

| Debt | Incurred Phase | Payback Phase | Impact of Not Paying |
|------|---------------|---------------|---------------------|
| IVFFlat instead of HNSW (pgvector) | P0 | P5a | Recall degrades after 50K inserts |
| Uni-temporal (not true bi-temporal) | P1 | P5b | Cannot query "what did the system believe at time X?" |
| Event-driven orchestration | P1 | P5a | Task chain failures harder to debug |
| No circuit breakers on external calls | P0 | P5a | Provider outage cascades to all workers |
| Single Redis (not Sentinel/Cluster) | P0 | P4 | ARQ queue lost on Redis restart |
| Direct PostgreSQL (no pgBouncer) | P0 | P4 | Connection exhaustion at 500 req/s |
| No PII redaction | P3 | P5a | PII sent to LLM in message content |
| No DLQ dashboard | P2 | P5b | Failed jobs invisible until Grafana check |
| BYOK LLM abstraction — user must configure on first run | P0 | P2 | New instance requires LLM configuration before first use |
| No air-gapped deployment | P4 | P5b | Self-hosted users need internet |

---

## Key Recommendations

1. **Do not start Phase 1 until Phase 0 exit criteria are met.** The exit criteria are concrete and measurable — if a gate fails, delay Phase 1.

2. **Prompt engineering is on the critical path.** Entity extraction (1.6) and fact extraction (1.8) require LLM prompt iteration. Start prompt design in week 4, before the worker code is written. Use golden datasets to eval prompt quality before merging.

3. **Graphiti version pinning is mandatory.** Pin `graphiti-core==0.29.1` and do NOT bump minor versions without a full regression run. Auto-upgrades will break the system silently.

4. **FalkorDB-first approach.** Run integration test suite against FalkorDB at least weekly to catch regressions early.

5. **SDKs must be test-driven from the OpenAPI spec.** Generate client stubs from `openapi.json`, fill in implementation. Never hand-write SDKs.

6. **Testing is not a phase — it's a practice.** Every PR must include tests. CI blocks merges on coverage regression. Test infrastructure is built in Phase 0, not Phase 5.

7. **Defensive cost controls from Day 1.** Set enrichment depth levels and per-org budgets in Phase 1, not Phase 3. LLM costs are the #1 operational risk.

8. **BYOK is the default — the system ships with no API key.** First-run auto-detects Ollama (local, zero-config) or prompts the user to configure any LLM provider (OpenAI, Azure, Anthropic). This is a deliberate design choice: no vendor lock-in, no shared key risk.

---

## Quick Reference: Transitioning Between Phases

| Transition | Must Be True Before Moving |
|-----------|---------------------------|
| **Phase 0 → Phase 1** | All 8 G0 gates pass. RLS proven under 100 concurrent connections. Cross-tenant matrix committed. |
| **Phase 1 → Phase 2** | All 10 G1 gates pass. Context assembly p99 ≤ 300ms (warm), ≤ 1500ms (cold). Cache hit rate ≥ 80%. |
| **Phase 2 → Phase 3** | All 9 G2 gates pass. Graph query p99 ≤ 500ms for 15k nodes. Python SDK published. MCP server verified. |
| **Phase 3 → Phase 4** | All 8 G3 gates pass. Cost controls operational. Eval suite runs on every merge. |
| **Phase 4 → Phase 5a** | All 6 G4 gates pass. Helm chart deploys on 3-node cluster. Dashboard usable. |
| **Phase 5a → Phase 5b** | All 9 G5a gates pass. Load test: 500 req/s × 5 min, all criteria met. Security: 0 critical/high. |
| **Phase 5b → Release** | All 7 G5b gates pass. All quality gates across ALL phases passing. Tech lead + Security sign-off. |

---

*Generated from SRS_MemGraph.md v1.0.0 and specialist reviews by @architect, @senior-dev, @reviewer, @qa-engineer, @junior-mentor, @devops.*
