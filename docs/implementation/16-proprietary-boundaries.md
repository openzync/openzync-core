# Proprietary Boundaries — Open-Source vs Source-Available IP

> **Document version:** 1.0.0
> **Status:** Draft
> **Author:** Rohan
> **Organisation:** TheLinkAI
> **Date:** 2026-06-05
> **Classification:** Internal / Engineering
> **Supersedes:** SRS Phase 5 licensing note (`LICENSE (Apache 2.0)`) — this document defines the definitive licensing strategy

---

## Table of Contents

1. [Licensing Strategy](#1-licensing-strategy)
2. [Open-Source Components (OSS/)](#2-open-source-components-oss)
3. [Proprietary Components (Core IP)](#3-proprietary-components-core-ip)
4. [Architecture: Separation Boundary](#4-architecture-separation-boundary)
5. [Dependency Flow — OSS → Proprietary](#5-dependency-flow--oss--proprietary)
6. [Monorepo Restructuring Plan](#6-monorepo-restructuring-plan)
7. [Implications](#7-implications)
8. [Migration Path from Current Layout](#8-migration-path-from-current-layout)
9. [FAQ](#9-faq)

---

## 1. Licensing Strategy

OpenZep uses a **dual-license model** that protects core competitive IP while maximising community adoption of the developer surface area.

### 1.1 The Two Licenses

| License | Applies To | Who Can Use | Key Obligation |
|---------|-----------|-------------|----------------|
| **AGPL v3** | Core platform source | Anyone | Modifications distributed as a networked service must release source code |
| **Commercial license** | Core platform source | Paying customers | No source release obligation — standard proprietary terms |

### 1.2 Rationale

This is the same model used by **MongoDB, Grafana, GitLab, and Sentry**. It strikes a balance between:

- **Open-source adoption** — developers can self-host for free, inspect the code, and contribute to non-core components
- **Commercial sustainability** — organisations that want to offer OpenZep as a SaaS without releasing their modifications under AGPL purchase a commercial license
- **Competitive moat** — the core algorithmic IP (context assembly, enrichment orchestration, prompt engineering) is never permissively licensed

### 1.3 Relationship to the SRS

The original SRS (Phase 5, Hardening) listed `LICENSE (Apache 2.0)` for the full project. This document supersedes that decision. The rationale:

- Apache 2.0 would allow competitors to take the core algorithms and offer them as proprietary SaaS without contributing back
- AGPL v3 closes this gap while still qualifying as "open source" under the OSI definition
- The `oss/` subtree receives a permissive license (MIT or Apache 2.0) to maximise community adoption of the SDK and API contracts

### 1.4 What This Means in Practice

| Scenario | License Applies |
|----------|----------------|
| Running self-hosted for internal use | AGPL v3 — free, no source release needed |
| Modifying core code and running as a public SaaS | AGPL v3 — must release modifications, OR buy commercial license |
| Building a proprietary SDK integration against `oss/` components | MIT/Apache 2.0 — no restrictions |
| Contributing a bug fix to `oss/repositories/` | MIT/Apache 2.0 — contribution under same license |
| Contributing a bug fix to `core/context-assembly/` | AGPL v3 — contribution under AGPL v3 |
| Embedding OpenZep in a proprietary appliance | Commercial license required |

---

## 2. Open-Source Components (OSS/)

These components live under `oss/` and are licensed under a **permissive license (MIT or Apache 2.0)**. They are the community-accessible surface area of OpenZep.

### 2.1 Component Table

| Component | Path | License | Why Open |
|-----------|------|---------|----------|
| Python SDK | `oss/sdk-python/` | Apache 2.0 | Community adoption — developers must integrate with it without legal friction |
| TypeScript SDK | `oss/sdk-typescript/` | Apache 2.0 | Community adoption — npm ecosystem expects permissive licensing |
| Go SDK | `oss/sdk-go/` | Apache 2.0 | Community adoption — Go module consumers expect permissive licensing |
| Pydantic schemas | `oss/schemas/` | MIT | API contract — must be visible for integration; also used by SDKs |
| FastAPI routers | `oss/routers/` | MIT | Thin HTTP adapters — no business logic, only request deserialisation and response serialisation |
| Repository classes | `oss/repositories/` | MIT | Data access layer — standard CRUD SQLAlchemy patterns, low IP value |
| MCP Server | `oss/mcp/` | Apache 2.0 | Protocol implementation — community contribution target; MCP is an open standard |
| Deployment configs | `oss/infra/` | MIT | Docker Compose, Helm chart — operational configuration, not business logic |
| Documentation | `oss/docs/` | MIT | Community adoption — docs must be freely accessible and forkable |
| OpenAPI spec | `oss/openapi.yaml` | MIT | API contract — auto-generated; integration tools depend on it |
| Database migrations | `oss/migrations/` | MIT | Alembic migration files — schema evolution is a mechanical concern |

### 2.2 Why These Are Open

**SDKs (Python, TypeScript, Go):** The SDK is the first thing a developer touches. If the SDK has a restrictive license, developers will evaluate competing solutions before they even try the product. Permissive licensing here removes an adoption barrier.

**Schemas and OpenAPI spec:** Integration tools (Postman, OpenAI's GPT actions, custom API clients) need the API contract to be freely redistributable. A restrictive license on schemas creates a legal grey area for tool builders.

**Routers:** FastAPI routers in this architecture are thin HTTP adapters — they validate input, call a service, and format a response. They contain zero business logic. Opening them lets the community see the full API surface and contribute endpoint improvements.

**Repositories:** SQLAlchemy CRUD patterns are mechanical. The IP value is in *how* data is composed and enriched, not in `SELECT` / `INSERT` statements. Opening repositories allows community contributors to add new query patterns and backends.

**MCP Server:** The MCP protocol is an open standard (Anthropic). Making the server implementation open-source encourages community contributions and makes OpenZep the default MCP memory provider.

**Infra configs:** Docker Compose files and Helm charts are operational boilerplate. Opening them reduces friction for self-hosters and encourages community-provided deployment options (e.g., Nomad, Ansible).

### 2.3 What OSS Components May NOT Do

- `oss/routers/` must NOT import from `services/`, `workers/`, or `prompts/`
- `oss/repositories/` must NOT import from `services/` or `core/` (except `core/exceptions.py` for standard error types — see exception carve-out below)
- `oss/schemas/` must NOT import from `models/` (ORM) — only Pydantic `BaseModel`
- `oss/` components must NOT contain any `.jinja2` prompt templates

**Exception:** `oss/repositories/` may import exception types from `core/exceptions.py` (e.g., `NotFoundError`, `ValidationError`) because these are part of the domain contract, not business logic. All other `core/` imports are prohibited.

---

## 3. Proprietary Components (Core IP)

These components live in the root `core/`, `services/`, `workers/`, and `prompts/` directories. They are **source-available under AGPL v3** or under a **commercial license** for customers who purchase one.

### 3.1 Component Table

| Component | Path | License | Why Proprietary |
|-----------|------|---------|-----------------|
| Context assembly engine | `core/context-assembly/` | AGPL v3 / Commercial | RRF merge weights, BFS scoring heuristics, context formatting — core competitive differentiator |
| Enrichment orchestration | `core/enrichment/` | AGPL v3 / Commercial | Orchestration DAG, enrichment status tracking, partial failure recovery — how the pipeline composes |
| LLM abstraction | `core/llm.py` | AGPL v3 / Commercial | BYOK provider abstraction — integration with Ollama, OpenAI, Azure — switching logic and retry strategies |
| Config & settings | `core/config.py` | AGPL v3 / Commercial | pydantic-settings with secrets management — environment variable schema is IP-agnostic but the implementation is tightly coupled to the platform |
| Exception hierarchy | `core/exceptions.py` | AGPL v3 / Commercial | Domain exception hierarchy that reflects business logic boundaries |
| DB engine | `core/db.py` | AGPL v3 / Commercial | Async engine setup, session factory, connection pooling strategy, pool tuning parameters |
| Logging | `core/logging.py` | AGPL v3 / Commercial | structlog config, PII redaction processor — security-critical configuration |
| All service classes | `services/` | AGPL v3 / Commercial | Business logic — how enrichment, retrieval, graph operations, and context assembly compose together |
| All worker tasks | `workers/tasks/` | AGPL v3 / Commercial | Enrichment job implementations — entity extraction, fact extraction, classification, summarisation orchestration |
| Worker base | `workers/worker.py` | AGPL v3 / Commercial | ARQ worker pool configuration, task registration, health checks |
| All prompt templates | `prompts/` | AGPL v3 / Commercial | `.jinja2` template files — prompt IP represents extensive tuning and domain expertise |
| PII detection | `core/pii/` | AGPL v3 / Commercial | Pre-LLM redaction engine — regex patterns, Spacy NER config, per-org configurable rules — compliance-critical IP |
| Embedding strategy | `core/embeddings.py` | AGPL v3 / Commercial | Embedding dispatch, batching logic, model fallback, dimension management |
| Cache strategy | `core/cache.py` | AGPL v3 / Commercial | Redis cache-aside pattern, TTL strategy, invalidation rules, hit-rate monitoring integration |
| Graph client abstraction | `core/graph-client/` | AGPL v3 / Commercial | FalkorDB / Neo4j backend abstraction, org-aware query prefixing, connection management |

### 3.2 Why These Are Proprietary

**Context assembly engine:** The algorithms that decide *what* to include in a context block, *how to rank* retrieved items (RRF weights), and *how to format* the output for optimal LLM consumption are the primary competitive differentiator. Open-sourcing this would let competitors replicate the core value proposition instantly.

**Enrichment orchestration:** The DAG that chains entity extraction → embedding → fact extraction → classification → community summarisation, including partial failure recovery and status tracking, represents significant engineering investment. The composition logic (what runs when, what happens on failure) is proprietary.

**Prompts:** Prompt templates are the result of extensive iteration, testing, and domain-specific tuning. They encode business logic about ontology extraction, fact formatting, and anti-hallucination guardrails. This is high-value IP.

**PII detection:** The pre-LLM redaction engine handles compliance-sensitive logic. Opening the detection rules could expose patterns that malicious actors would probe for blind spots.

### 3.3 Boundary Carve-Outs

The following files are strictly internal and must NEVER be referenced from `oss/`:

| File | Reason |
|------|--------|
| `private/config.yaml` | Per-org enrichment depth, LLM budget thresholds, feature flags |
| `private/prompt-overrides/` | Organisation-specific prompt customisation — contains client ontologies |
| `private/integration-secrets/` | Third-party API credentials, webhook signing keys |

These live outside the repository in a private `openzep-private` repo or a Vault instance.

---

## 4. Architecture: Separation Boundary

The OSS vs proprietary boundary maps cleanly onto the physical directory structure. Every developer (internal and external) can see at a glance what is open and what is not.

### 4.1 Directory Layout

```
openzep/
│
├── oss/                              # OPEN SOURCE — MIT / Apache 2.0
│   ├── sdk-python/                   # Python client library (PyPI)
│   │   ├── openzep/                 # SDK package
│   │   ├── tests/
│   │   ├── pyproject.toml
│   │   └── README.md
│   │
│   ├── sdk-typescript/               # TypeScript client library (npm)
│   │   ├── src/
│   │   ├── tests/
│   │   ├── package.json
│   │   └── README.md
│   │
│   ├── sdk-go/                       # Go client library (pkg.go.dev)
│   │   ├── openzep/
│   │   ├── tests/
│   │   ├── go.mod
│   │   └── README.md
│   │
│   ├── schemas/                      # Pydantic request/response models
│   │   ├── memory.py
│   │   ├── graph.py
│   │   ├── user.py
│   │   ├── session.py
│   │   ├── fact.py
│   │   ├── search.py
│   │   └── common.py                 # Shared base models, pagination, errors
│   │
│   ├── routers/                      # FastAPI route handlers (thin HTTP adapters)
│   │   ├── memory.py                 # POST /memory, GET /context, DELETE /memory
│   │   ├── graph.py                  # Graph CRUD endpoints
│   │   ├── user.py                   # User CRUD endpoints
│   │   ├── session.py                # Session CRUD endpoints
│   │   ├── fact.py                   # Fact CRUD endpoints
│   │   ├── search.py                 # Hybrid search endpoint
│   │   ├── admin.py                  # Admin endpoints (org management, API keys)
│   │   └── health.py                 # Health check — PUBLIC ENDPOINT, no auth
│   │
│   ├── repositories/                 # SQLAlchemy data access
│   │   ├── base.py                   # Abstract base repository with common CRUD
│   │   ├── user_repo.py
│   │   ├── session_repo.py
│   │   ├── episode_repo.py
│   │   ├── fact_repo.py
│   │   ├── extraction_repo.py
│   │   ├── classification_repo.py
│   │   ├── organization_repo.py
│   │   └── api_key_repo.py
│   │
│   ├── mcp/                          # MCP server (protocol implementation)
│   │   ├── server.py                 # Stdio + SSE transport
│   │   ├── tools/                    # Tool definitions
│   │   │   ├── add_memory.py
│   │   │   ├── get_context.py
│   │   │   ├── search_memory.py
│   │   │   ├── add_fact.py
│   │   │   ├── list_facts.py
│   │   │   ├── get_user_graph.py
│   │   │   ├── create_user.py
│   │   │   └── list_sessions.py
│   │   └── auth.py                   # API key auth for MCP
│   │
│   ├── infra/                        # Deployment configuration
│   │   ├── docker-compose.yml
│   │   ├── docker-compose.prod.yml
│   │   ├── helm/                     # Kubernetes Helm chart
│   │   │   ├── Chart.yaml
│   │   │   ├── values.yaml
│   │   │   └── templates/
│   │   ├── Dockerfile
│   │   └── .dockerignore
│   │
│   ├── migrations/                   # Alembic migration files
│   │   ├── env.py
│   │   ├── script.py.mako
│   │   └── versions/
│   │
│   ├── docs/                         # Public documentation
│   │   ├── README.md
│   │   ├── CONTRIBUTING.md
│   │   ├── getting-started.md
│   │   ├── deployment.md
│   │   └── api/                      # Generated OpenAPI docs
│   │
│   └── openapi.yaml                  # OpenAPI 3.1 spec (auto-generated)
│
├── core/                             # PROPRIETARY — AGPL v3 / Commercial
│   ├── __init__.py
│   ├── config.py                     # pydantic-settings, secrets management
│   ├── db.py                         # Async engine, session factory, pool config
│   ├── exceptions.py                 # Domain exception hierarchy
│   ├── logging.py                    # structlog config, PII redaction
│   ├── llm.py                        # BYOK abstraction (Ollama, OpenAI, Azure)
│   ├── embeddings.py                 # Embedding dispatch, batching, fallback
│   ├── cache.py                      # Redis cache-aside, TTL strategy, invalidation
│   ├── telemetry.py                  # OpenTelemetry setup, metric registry
│   ├── context-assembly/             # RRF merge, BFS scoring, formatting
│   │   ├── __init__.py
│   │   ├── rrf_ranker.py             # Reciprocal Rank Fusion implementation
│   │   ├── bfs_traverser.py          # BFS graph traversal with edge-weight scoring
│   │   ├── context_formatter.py      # Formats retrieved items into LLM-ready block
│   │   └── weights.py                # Tunable RRF and BFS scoring weights
│   │
│   ├── enrichment/                   # Orchestration DAG
│   │   ├── __init__.py
│   │   ├── orchestrator.py           # DAG chaining, status tracking
│   │   ├── recovery.py               # Partial failure recovery logic
│   │   └── status.py                 # Enrichment status state machine
│   │
│   ├── graph-client/                 # Graph backend abstraction
│   │   ├── __init__.py
│   │   ├── base.py                   # Abstract graph client interface
│   │   ├── falkordb_client.py        # FalkorDB implementation
│   │   ├── neo4j_client.py           # Neo4j implementation
│   │   └── org_prefix.py             # Org-aware key/namespace prefixing
│   │
│   ├── pii/                          # Pre-LLM PII detection/redaction
│   │   ├── __init__.py
│   │   ├── detector.py               # PII detection engine
│   │   ├── redactor.py               # PII redaction (masking/replacement)
│   │   ├── patterns.py               # Regex patterns for common PII types
│   │   └── config.py                 # Per-org PII rules
│   │
│   └── security/                     # Security-critical internals
│       ├── __init__.py
│       ├── rate_limiter.py           # Redis-backed rate limiter
│       ├── key_service.py            # API key generation, hashing, validation
│       └── audit.py                  # Audit logging for security events
│
├── services/                         # PROPRIETARY — AGPL v3 / Commercial
│   ├── __init__.py
│   ├── memory_service.py             # Message ingestion orchestration
│   ├── context_service.py            # Context assembly orchestration
│   ├── retrieval_service.py          # Hybrid retrieval coordination
│   ├── graph_service.py              # Graph query orchestration
│   ├── user_service.py               # User CRUD business logic
│   ├── session_service.py            # Session management business logic
│   ├── fact_service.py               # Fact extraction orchestration
│   ├── search_service.py             # Cross-domain search coordination
│   ├── admin_service.py              # Tenant management business logic
│   └── enrichment_service.py         # Enrichment pipeline orchestration
│
├── workers/                          # PROPRIETARY — AGPL v3 / Commercial
│   ├── __init__.py
│   ├── worker.py                     # ARQ worker pool, task registration
│   └── tasks/
│       ├── __init__.py
│       ├── extract_entities.py       # Entity extraction worker
│       ├── embed_episode.py          # Episode embedding worker
│       ├── embed_entity.py           # Entity embedding worker
│       ├── extract_facts.py          # Fact extraction worker
│       ├── classify_dialog.py        # Dialog classification worker
│       ├── extract_structured.py     # Structured data extraction worker
│       ├── summarise_community.py    # Community summarisation worker
│       └── ingest_business_data.py   # Business data ingestion worker
│
├── prompts/                          # PROPRIETARY — AGPL v3 / Commercial
│   ├── extract_entities_v1.jinja2
│   ├── extract_facts_v1.jinja2
│   ├── classify_intent_v1.jinja2
│   ├── classify_emotion_v1.jinja2
│   ├── extract_structured_v1.jinja2
│   ├── summarise_community_v1.jinja2
│   └── anti_hallucination_v1.jinja2  # Guardrail prompt
│
├── apps/                             # PROPRIETARY — AGPL v3 / Commercial
│   └── dashboard/                    # Next.js admin dashboard
│       ├── app/
│       ├── components/
│       ├── package.json
│       └── ...
│
├── tests/                            # Test organisation mirrors source
│   ├── oss/                          # Tests for oss/ components — community-run
│   │   ├── test_schemas/
│   │   ├── test_repositories/
│   │   └── test_mcp/
│   ├── core/                         # Tests for core/ — internal only
│   ├── services/                     # Tests for services/ — internal only
│   ├── workers/                      # Tests for workers/ — internal only
│   ├── integration/                  # Cross-component integration tests
│   └── conftest.py
│
├── scripts/                          # PROPRIETARY — AGPL v3 / Commercial
│   ├── migrate.py                    # DB migration runner
│   ├── seed.py                       # Dev data seeding
│   └── benchmark.py                  # Performance benchmark harness
│
├── docs/                             # Internal implementation docs
│   └── implementation/               # ADRs, runbooks, API contracts
│
├── pyproject.toml                    # Root project config
├── Makefile
├── .env.example
├── .gitignore
├── LICENSE                           # AGPL v3 (root) — see also oss/LICENSE
├── oss/LICENSE                       # MIT (for oss/ subtree)
└── README.md
```

### 4.2 Visual Boundary

```
                    ┌─────────────────────────────────────┐
                    │         COMMUNITY / EXTERNAL          │
                    │  oss/ — MIT / Apache 2.0              │
                    │                                       │
                    │  sdk-python/  sdk-typescript/  sdk-go/│
                    │  schemas/     routers/                │
                    │  repositories/  mcp/                  │
                    │  infra/   migrations/  docs/          │
                    │  openapi.yaml                         │
                    └──────────────┬──────────────────────┘
                                   │
                    ╔══════════════╪══════════════════════╗
                    ║              │  IMPORT ONLY         ║
                    ║              ▼                      ║
                    ║  ┌─────────────────────────┐        ║
                    ║  │ services/                │        ║
                    ║  │  (business logic layer)   │        ║
                    ║  └──────────┬──────────────┘        ║
                    ║             │                        ║
                    ║             ▼                        ║
                    ║  ┌─────────────────────────┐        ║
                    ║  │ core/                    │        ║
                    ║  │  (engine, infra, sec)    │        ║
                    ║  └─────────────────────────┘        ║
                    ║                                     ║
                    ║  ┌─────────────────────────┐        ║
                    ║  │ workers/                 │        ║
                    ║  │  (async enrichment)      │        ║
                    ║  └─────────────────────────┘        ║
                    ║                                     ║
                    ║  ┌─────────────────────────┐        ║
                    ║  │ prompts/                 │        ║
                    ║  │  (prompt IP)             │        ║
                    ║  └─────────────────────────┘        ║
                    ║                                     ║
                    ║  PROPRIETARY — AGPL v3 / Commercial ║
                    ╚═════════════════════════════════════╝
```

---

## 5. Dependency Flow — OSS → Proprietary

The dependency direction is **strictly one-way**: proprietary code depends on OSS code, never the reverse.

### 5.1 Dependency Graph

```
oss/schemas/
    ↑
oss/repositories/    ←←← reads/writes DB (no business logic)
    ↑
oss/routers/         ←←← imports schemas, calls services
    ↑
services/            ←←← imports repositories, calls core/engines
    ↑
core/                ←←← foundation layer: config, db, llm, cache
    ↑
workers/             ←←← imports services, core
    ↑
prompts/             ←←← loaded by workers/services (template references)

(No arrow points from oss/ to services/, workers/, or prompts/)
```

### 5.2 Import Rules (Non-Negotiable)

| Source | May Import From | Must NOT Import From |
|--------|----------------|---------------------|
| `oss/schemas/` | stdlib, pydantic, typing | `models/`, `services/`, `core/`, `workers/`, `prompts/` |
| `oss/routers/` | `oss/schemas/`, `core/exceptions.py` | `services/`, `workers/`, `core/` (except exceptions) |
| `oss/repositories/` | `oss/schemas/`, `models/`, `core/exceptions.py` | `services/`, `core/` (except exceptions), `workers/`, `prompts/` |
| `oss/mcp/` | `oss/schemas/`, `services/` | `core/` (directly), `workers/`, `prompts/` |
| `services/` | `oss/repositories/`, `oss/schemas/`, `core/` | `workers/`, `prompts/` (directly) |
| `workers/` | `services/`, `core/`, `oss/repositories/` | `prompts/` (uses via service) |
| `core/` | `oss/schemas/` (for types), stdlib | `services/`, `workers/`, `prompts/` |
| `prompts/` | nothing (template files) | Logic must not be embedded in prompts |

### 5.3 Enforcement

These rules are enforced at three levels:

1. **`pyproject.toml` dependency groups** — `oss/` components declare only their allowed dependencies
2. **CI lint rule** — `scripts/check-import-boundaries.py` runs in CI and fails if any `oss/` file imports from `services/`, `workers/`, or `core/` (except the exceptions carve-out)
3. **Code review** — any PR that violates the dependency direction is an automatic rejection

### 5.4 Why This Matters

- **OSS components can be compiled and published independently** of proprietary code. The Python SDK, for example, depends only on `oss/schemas/` and standard library — it can be built and published to PyPI without access to `core/` or `services/`.
- **External contributors can submit PRs to `oss/` without seeing proprietary code.** Their development environment only needs the `oss/` subtree.
- **The proprietary layer can be refactored freely** without breaking OSS components, as long as the service interfaces remain stable.
- **CI can run OSS tests in isolation** without proprietary dependencies, giving fast feedback to community contributors.

---

## 6. Monorepo Restructuring Plan

The SRS monorepo layout (from `SRS_MemGraph.md` §4.2) uses a `services/api/routers/` nesting. This document introduces a flat `oss/` top-level directory. This section describes the migration.

### 6.1 Current Layout (SRS §4.2)

```
OpenZep/
├── services/
│   ├── api/
│   │   ├── routers/          # → moves to oss/routers/
│   │   ├── dependencies/     # → moves to oss/routers/dependencies/ or core/
│   │   ├── middleware/       # → moves to core/ (proprietary)
│   │   └── main.py           # → stays at root or oss/
│   ├── worker/               # → workers/ (proprietary)
│   │   ├── tasks/            # → workers/tasks/
│   │   └── worker.py         # → workers/worker.py
│   └── mcp/                  # → oss/mcp/
├── packages/
│   ├── core/                 # → core/ (proprietary)
│   ├── graphiti-client/      # → core/graph-client/
│   ├── sdk-python/           # → oss/sdk-python/
│   ├── sdk-typescript/       # → oss/sdk-typescript/
│   └── sdk-go/               # → oss/sdk-go/
├── apps/
│   └── dashboard/            # → apps/dashboard/ (proprietary)
├── infra/                    # → oss/infra/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── e2e/
└── docs/
    └── openapi.yaml          # → oss/openapi.yaml
```

### 6.2 Target Layout (This Document)

```
openzep/
├── oss/                      # All OSS code in one tree
│   ├── sdk-python/
│   ├── sdk-typescript/
│   ├── sdk-go/
│   ├── schemas/
│   ├── routers/
│   ├── repositories/
│   ├── mcp/
│   ├── infra/
│   ├── migrations/
│   ├── docs/
│   └── openapi.yaml
├── core/                     # Proprietary engines & infra
├── services/                 # Proprietary business logic
├── workers/                  # Proprietary async tasks
├── prompts/                  # Proprietary prompt IP
├── apps/                     # Proprietary dashboard
├── tests/                    # Mirrors source layout
│   ├── oss/
│   ├── core/
│   ├── services/
│   ├── workers/
│   └── integration/
└── scripts/                  # Proprietary helper scripts
```

### 6.3 Package Names & Imports During Transition

To allow a gradual migration without breaking imports mid-flight, use `openzep.` as the root package:

| Current Import | New Import (Phase 1 — both work) | Final Import |
|----------------|-----------------------------------|--------------|
| `from services.api.routers import ...` | `from openzep.oss.routers import ...` | Same |
| `from packages.core import ...` | `from openzep.core import ...` | Same |
| `from packages.sdk_python import ...` | `from openzep.oss.sdk_python import ...` | Same |

Phase 1: Add the new structure alongside the old one, with symlinks or import redirects.
Phase 2: Update all internal imports to the new paths.
Phase 3: Remove the old structure.

This is a non-breaking refactoring — no external consumer should notice the change.

---

## 7. Implications

### 7.1 For External Contributors

| Activity | Allowed? | License | Notes |
|----------|----------|---------|-------|
| Submit PR to `oss/sdk-python/` | Yes | Apache 2.0 | Standard GitHub fork + PR workflow |
| Submit PR to `oss/schemas/` | Yes | MIT | Schema validation tests must pass |
| Submit PR to `oss/routers/` | Yes | MIT | Must not import proprietary code |
| Submit PR to `oss/repositories/` | Yes | MIT | Must not contain business logic |
| Submit PR to `oss/mcp/` | Yes | Apache 2.0 | MCP is an open standard |
| Submit PR to `oss/infra/` | Yes | MIT | Docker/Helm configs |
| Submit PR to `oss/docs/` | Yes | MIT | Documentation improvements |
| Submit bug report for `core/` | Yes | N/A | Issue tracker — reproduction steps required |
| Submit PR to `core/` | No | — | Internal only; security/legal review needed |
| Submit PR to `services/` | No | — | Internal only |
| Submit PR to `workers/` | No | — | Internal only |
| Submit PR to `prompts/` | No | — | Internal only |

### 7.2 For Internal Developers

| Activity | Constraint |
|----------|-----------|
| Modifying `oss/` code | Must maintain backward compatibility; PR reviewed against OSS standards |
| Adding a new repository | Must go in `oss/repositories/` unless it contains business logic |
| Adding a new router | Must go in `oss/routers/` unless it requires proprietary dependencies |
| Adding a new service | Must go in `services/` — always proprietary |
| Adding a new worker task | Must go in `workers/tasks/` — always proprietary |
| Adding a new prompt | Must go in `prompts/` — always proprietary |
| Refactoring `core/` | Must not break `services/` interfaces; tests must all pass |
| Changing `oss/schemas/` | Must update `oss/openapi.yaml`; SDKs may need regeneration |

### 7.3 Bug Fix Flow

**Bug in `oss/` (e.g., router returns wrong status code):**
- Fix is public on the main branch
- External contributors can submit the fix
- CI runs OSS test suite only for community PRs

**Bug in `core/` (e.g., RRF merge produces wrong ranking):**
- Fix is internal only
- Reported via private issue tracker
- Fix committed directly to main; changelog entry describes the bug (not the algorithm detail)

**Bug that spans OSS + proprietary (e.g., wrong error response from a service):**
- Fix the proprietary service layer
- Verify the OSS schema/router accurately reflects the new behaviour
- Update `oss/openapi.yaml` if the error contract changed

### 7.4 Commercial License Implications

| Aspect | Detail |
|--------|--------|
| **What the buyer gets** | Access to the full proprietary codebase with the right to modify and operate without AGPL source release obligations |
| **What the buyer does NOT get** | Exclusive rights — the code remains public under AGPL; only the license terms differ |
| **SaaS use case** | Commercial license required if offering OpenZep as a managed service without AGPL compliance |
| **Embedded use case** | Commercial license required if embedding OpenZep in a proprietary appliance or product |
| **Support and SLA** | Commercial license typically bundled with support contract (separate from this document) |
| **Pricing model** | Out of scope for this document — defined in commercial contracts |

### 7.5 SDK Licensing Specifics

The SDKs are fully open-source (Apache 2.0) specifically to avoid blocking community adoption:

- **Python SDK** can be pip-installed without any license friction
- **TypeScript SDK** can be npm-installed in any project (MIT-licensed or proprietary)
- **Go SDK** can be `go get`-ed in any project
- Community members can fork the SDKs, extend them, and publish alternative versions
- Integration examples in third-party frameworks (LangChain, LlamaIndex, OpenAI Agents SDK) can reference the SDK without legal concerns

### 7.6 API Contract Licensing Specifics

The schemas and OpenAPI spec are MIT-licensed:

- API clients can be auto-generated from the OpenAPI spec by any tool (Postman, openapi-generator, custom scripts)
- Third-party dashboard builders can integrate against the API without licensing concerns
- The API contract is the "interface" — it must be freely available for interoperability

---

## 8. Migration Path from Current Layout

The existing codebase (as of `SRS_MemGraph.md` v1.0.0) uses a different monorepo layout. This section describes the step-by-step migration to the `oss/` boundary structure.

### 8.1 Phase 0 — Directory Restructuring (Week 0)

| Current Path | New Path | Action |
|-------------|----------|--------|
| `services/api/routers/` | `oss/routers/` | Move directory |
| `services/api/dependencies/` | `oss/routers/dependencies/` | Move directory (auth DI is OSS contract) |
| `services/api/middleware/` | `core/middleware/` | Move — instrumentation is proprietary |
| `services/api/main.py` | `app/main.py` | Move — app creation stays root-level |
| `packages/core/` | `core/` | Flatten — remove `packages/` nesting |
| `packages/graphiti-client/` | `core/graph-client/` | Rename and flatten |
| `packages/sdk-python/` | `oss/sdk-python/` | Move — SDK is OSS |
| `packages/sdk-typescript/` | `oss/sdk-typescript/` | Move — SDK is OSS |
| `packages/sdk-go/` | `oss/sdk-go/` | Move — SDK is OSS |
| `services/worker/` | `workers/` | Rename and flatten |
| `services/mcp/` | `oss/mcp/` | Move — MCP is OSS |
| `infra/` | `oss/infra/` | Move — infra configs are OSS |
| `docs/openapi.yaml` | `oss/openapi.yaml` | Move |
| `apps/dashboard/` | `apps/dashboard/` | Keep (proprietary) |

### 8.2 Phase 1 — Import Path Updates (Week 0-1)

- Update all `pyproject.toml` and `setup.py` files to use `openzep.` as the root namespace
- Add import compatibility shims at old paths (deprecation warnings)
- Script: `scripts/update-imports.py` — automated import path migration

### 8.3 Phase 2 — CI Boundary Enforcement (Week 1)

- Add `scripts/check-import-boundaries.py` to CI pipeline
- Add LICENSE files: `LICENSE` (AGPL v3, root) + `oss/LICENSE` (MIT)
- Configure PyPI/npm publishing to only include `oss/` subtrees
- Set up branch protection: only internal team can merge to `services/`, `core/`, `workers/`, `prompts/`

### 8.4 Phase 3 — Old Path Removal (Week 2+)

- Remove deprecated import shims after all internal code is updated
- Archive old layout to a branch for reference

---

## 9. FAQ

### Why AGPL v3 and not Apache 2.0?

Apache 2.0 would allow a competitor to take OpenZep's core algorithms (context assembly with RRF weights, enrichment DAG orchestration, prompt templates) and offer them as a proprietary SaaS without contributing any improvements back to the community. AGPL v3 closes this gap: any organisation that modifies and runs OpenZep as a network service must release their modifications. Organisations that prefer not to release modifications can purchase a commercial license.

### Why not SSPL (MongoDB) or BSL (Redis)?

SSPL and BSL are not OSI-approved open-source licenses. Using them would mean OpenZep cannot call itself "open source," which harms community adoption. AGPL v3 is OSI-approved and achieves the same practical protection without the branding cost.

### Can a contributor accidentally violate the AGPL?

External contributors can only submit PRs to `oss/` directories (MIT/Apache 2.0). The AGPL v3 code is internal-only. A contributor cannot accidentally accept AGPL obligations because they cannot modify AGPL-licensed files through the standard PR process.

### What if someone wants to contribute a feature that touches both OSS and proprietary code?

The contributor submits the OSS portion (e.g., a new router endpoint in `oss/routers/` and new schemas in `oss/schemas/`). The internal team implements the corresponding service layer in `services/` and core logic in `core/`. The contributor never needs to see the proprietary implementation.

### Can an organisation use OpenZep under AGPL v3 for internal tools?

Yes. AGPL v3's network-use-as-distribution clause applies when the software is used to provide a service to third parties. Internal company tools, dev/staging environments, and employee-only applications are free under AGPL v3 with no source release obligation.

### What happens if Graphiti changes its license?

Graphiti (Apache 2.0) is embedded as a library dependency. If it changes license, we evaluate compatibility. In the worst case, we fork the last Apache 2.0 version and maintain it internally. This risk is already flagged in the SRS (OQ-01).

### How do we handle dual-license headers?

Every source file carries a SPDX header:

```python
# SPDX-License-Identifier: MIT OR Apache-2.0
# oss/repositories/user_repo.py — Open Source
```

```python
# SPDX-License-Identifier: AGPL-3.0-only
# If you have a commercial license, see LICENSE.commercial
# services/memory_service.py — Proprietary
```

The root `README.md` clearly states the dual-license model and directs readers to `oss/` for the permissively-licensed subset.

### Where does the admin dashboard fall?

The Next.js admin dashboard (`apps/dashboard/`) is AGPL v3 / commercial. It does not contain algorithmic IP, but it exposes admin functionality (tenant management, API key generation, user data browsing) that is part of the managed service value proposition. If the community requests an OSS dashboard, we can spin out a read-only version under MIT.

---

## Appendix A: License Compatibility Matrix

| Dependency | License | Compatible with AGPL v3? | Compatible with MIT? | Notes |
|-----------|---------|-------------------------|---------------------|-------|
| Graphiti | Apache 2.0 | Yes | Yes | Embedded library |
| FastAPI | MIT | Yes | Yes | Framework |
| SQLAlchemy | MIT | Yes | Yes | ORM |
| ARQ | MIT | Yes | Yes | Worker queue |
| Pydantic | MIT | Yes | Yes | Validation |
| structlog | Apache 2.0 / MIT | Yes | Yes | Logging |
| OpenTelemetry | Apache 2.0 | Yes | Yes | Tracing |
| FalkorDB | SSPL | Use as external service | Use as external service | Not embedded — AGPL does not require copyleft on external services |
| Neo4j | GPL v3 / AGPL v3 | Yes | Yes | Community edition |
| Redis | RSALv2 | Use as external service | Use as external service | Not embedded — AGPL does not require copyleft on external services |
| PostgreSQL | PostgreSQL License | Yes | Yes | Not linked — client-server protocol |

Libraries embedded in the OpenZep process must be license-compatible with AGPL v3. Services accessed over the network (PostgreSQL, Redis, FalkorDB) are not affected by OpenZep's license.

---

## Appendix B: CI Boundary Check Script Specification

A CI check (`scripts/check-import-boundaries.py`) must enforce the following rules:

```
Rule 1: No oss/ file may import from services/, workers/, or prompts/
Rule 2: No oss/ file may import from core/ except core/exceptions.py
Rule 3: No oss/schemas/ file may import from models/ (ORM)
Rule 4: No services/ file may import from workers/ or prompts/ directly
Rule 5: No core/ file may import from services/, workers/, or prompts/
Rule 6: Repository files (oss/repositories/) must not import from core/ except exceptions
```

Implementation approach: AST-parsing of all `.py` files, extracting `import` and `from ... import` statements, resolving them against the project's `openzep.` namespace, and asserting the rules above. Fail CI on any violation.

```python
# Pseudo-code for scripts/check-import-boundaries.py
import ast
import sys
from pathlib import Path

FORBIDDEN_IMPORTS = {
    "oss/": ["services", "workers", "prompts", "core"],
    "oss/schemas/": ["models"],
    "core/": ["services", "workers", "prompts"],
}

ALLOWED_OSS_CORE_IMPORTS = {"core/exceptions"}

def check_file(filepath: Path) -> list[str]:
    violations = []
    with open(filepath) as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for prefix, forbidden in FORBIDDEN_IMPORTS.items():
                if str(filepath).startswith(prefix):
                    module = node.module or ""
                    for f_mod in forbidden:
                        if module.startswith(f"openzep.{f_mod}"):
                            if module not in ALLOWED_OSS_CORE_IMPORTS:
                                violations.append(...)
    return violations

# ... (full implementation in scripts/check-import-boundaries.py)
```

---

## Appendix C: SPDX Header Templates

**OSS files (MIT / Apache 2.0) — `oss/`:**

```python
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 TheLinkAI Pvt. Ltd.
# oss/schemas/memory.py — Open Source — see oss/LICENSE for terms
```

**Proprietary files (AGPL v3 / Commercial) — `core/`, `services/`, `workers/`, `prompts/`:**

```python
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 TheLinkAI Pvt. Ltd.
# services/memory_service.py — Proprietary
# If you have a commercial license, see LICENSE.commercial for alternative terms.
```

**Test files:**
```python
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 TheLinkAI Pvt. Ltd.
# tests/oss/test_schemas.py
```

---

*Document maintained by Rohan · TheLinkAI · Last updated: 2026-06-05*
*Supersedes licensing references in `SRS_MemGraph.md` §14 (Phase 5) and earlier implementation docs.*
