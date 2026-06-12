# Software Requirements Specification (SRS)

## OpenZep — Open-Source Agent Memory Platform

---

| Field | Detail |
|---|---|
| **Document version** | 1.0.0 |
| **Status** | Draft |
| **Author** | Rohan |
| **Organisation** | TheLinkAI |
| **Date** | 2026-06-05 |
| **Classification** | Internal / Engineering |

---

## Table of Contents

1. [Introduction](#1-introduction)
2. [Overall Description](#2-overall-description)
3. [Stakeholders](#3-stakeholders)
4. [System Architecture Overview](#4-system-architecture-overview)
5. [Functional Requirements](#5-functional-requirements)
   - 5.1 Authentication & Multi-Tenancy
   - 5.2 Memory Ingestion
   - 5.3 Knowledge Graph Management
   - 5.4 Context Assembly & Retrieval
   - 5.5 NLP Enrichment Pipeline
   - 5.6 User & Session Management
   - 5.7 Async Worker System
   - 5.8 SDKs
   - 5.9 MCP Server
   - 5.10 Admin Dashboard
6. [Non-Functional Requirements](#6-non-functional-requirements)
7. [Data Models](#7-data-models)
8. [API Specification](#8-api-specification)
9. [Technology Stack](#9-technology-stack)
10. [Infrastructure & Deployment](#10-infrastructure--deployment)
11. [Observability](#11-observability)
12. [Security Requirements](#12-security-requirements)
13. [Competitive Feature Parity Matrix](#13-competitive-feature-parity-matrix)
14. [Build Phases & Milestones](#14-build-phases--milestones)
15. [Open Questions & Risks](#15-open-questions--risks)
16. [Glossary](#16-glossary)

---

## 1. Introduction

### 1.1 Purpose

This document defines the complete software requirements for **OpenZep**, a fully open-source, self-hostable agent memory platform. OpenZep replicates and extends the feature set of [Zep.ai](https://getzep.com) — specifically Zep Cloud — by combining a temporal knowledge graph engine, hybrid retrieval, NLP enrichment, multi-tenant user management, and developer SDKs into a single deployable platform.

### 1.2 Scope

OpenZep provides:

- A **REST API** for persisting and retrieving agent memory across sessions
- A **temporal knowledge graph** (powered by Graphiti, Apache 2.0) tracking entities, relationships, and their validity over time
- A **hybrid retrieval engine** combining vector similarity, BM25 full-text, and graph traversal
- An **NLP enrichment pipeline** for fact extraction, dialog classification, and structured data extraction
- **Python, TypeScript, and Go SDKs** mirroring Zep's client DX
- An **MCP server** for native tool integration with LLM agents
- A **Next.js admin dashboard** for managing users, graphs, and usage analytics
- Full self-hosting with Docker Compose and Kubernetes Helm chart

### 1.3 Intended Audience

| Audience | Usage |
|---|---|
| Backend engineers | API integration, SDK usage |
| AI/ML engineers | Agent memory integration, graph queries |
| DevOps / Platform | Deployment, infra configuration |
| Product / Tech leads | Feature planning, milestone tracking |

### 1.4 Definitions

See [Section 16 — Glossary](#16-glossary).

### 1.5 References

- [Graphiti GitHub](https://github.com/getzep/graphiti) — Apache 2.0, temporal KG engine
- [Zep documentation](https://docs.getzep.com) — feature parity baseline
- [Mem0 research](https://arxiv.org/abs/2504.19590) — retrieval benchmarks
- [FalkorDB](https://falkordb.com) — Redis-protocol graph database
- Internal: `agents.md` engineering standards, TheLinkAI

---

## 2. Overall Description

### 2.1 Product Perspective

LLM agents are stateless by default — each conversation starts without memory of prior interactions, user preferences, or accumulated knowledge. OpenZep solves this by acting as a persistent, structured memory layer sitting between the application layer and the LLM. Unlike naive vector stores, OpenZep models memory as a temporal knowledge graph: facts have timestamps, validity windows, and typed relationships, enabling agents to reason about *what was true when*.

```
Application / Agent
        │
        ▼
  OpenZep API
  ┌──────────────────────────────┐
  │  Ingest  │  Retrieve │  Query │
  └──────────────────────────────┘
        │
   Graphiti TKG Engine
   ┌──────────────────────────┐
   │ Episodic │ Semantic │ Temporal│
   └──────────────────────────┘
        │
  Storage Layer
  PG + pgvector │ FalkorDB │ Redis
```

### 2.2 Product Functions (Summary)

- Persist raw conversation sessions (episodic memory)
- Auto-extract entities, relationships, and facts from messages
- Store facts with bi-temporal validity windows (valid-time + transaction-time)
- Cluster entities into community summaries for long-context compression
- Retrieve context blocks optimised for the LLM's context window via hybrid search
- Accept arbitrary business data (transactions, tickets, emails) as graph facts
- Expose classification and structured extraction on conversations
- Provide multi-tenant API with per-org graph namespacing
- Serve all of the above via REST, Python SDK, TypeScript SDK, Go SDK, and MCP tools

### 2.3 User Classes

| User Class | Description |
|---|---|
| **Agent developer** | Integrates OpenZep into AI agents via SDK or REST |
| **Platform admin** | Manages tenants, API keys, usage quotas via dashboard |
| **End user (indirect)** | Person whose conversations are stored; has no direct OpenZep access |
| **DevOps engineer** | Deploys and operates OpenZep infrastructure |

### 2.4 Operating Environment

- Linux server (Ubuntu 22.04+ recommended) or Kubernetes cluster
- Docker Compose (single-node dev/staging), Helm chart (production)
- Python 3.11+, Node.js 20+ (for dashboard)
- LLM backend: OpenAI API, Azure OpenAI, or local Ollama
- Embedding backend: OpenAI `text-embedding-3-small`, or local `nomic-embed-text` via Ollama

### 2.5 Design and Implementation Constraints

- All server-side code written in **Python** (FastAPI)
- Background jobs use **ARQ** (Redis-backed async queue — already standardised at TheLinkAI)
- Graphiti engine embedded as a library, not a sidecar
- Graph database must support Redis-protocol or standard TCP socket (no cloud-only backends)
- No mandatory external SaaS dependency — fully air-gappable with Ollama
- All public APIs must be versioned under `/v1/`

### 2.6 Assumptions and Dependencies

- Graphiti (Apache 2.0) remains maintained and API-stable
- FalkorDB or Neo4j available as graph backend
- PostgreSQL 15+ with pgvector extension for vector similarity
- Redis 7+ for job queue and caching
- Deploying organisation has access to an LLM API or local Ollama instance

---

## 3. Stakeholders

| Stakeholder | Role | Interest |
|---|---|---|
| Rohan | Tech Lead, TheLinkAI | Primary owner, architecture and delivery |
| TheLinkAI engineers | Backend team | SDK consumers, code contributors |
| External agent developers | Community users | REST/SDK consumers, feature requests |
| Open-source community | Contributors | Bug fixes, new graph backends, SDKs |

---

## 4. System Architecture Overview

### 4.1 Component Map

```
┌─────────────────────────────────────────────────────────────────┐
│                          Clients                                  │
│     Python SDK · TypeScript SDK · Go SDK · MCP · REST            │
└────────────────────────────┬────────────────────────────────────┘
                             │ HTTPS
┌────────────────────────────▼────────────────────────────────────┐
│                      FastAPI Gateway                              │
│   Auth (API key + JWT) · Rate limiting · Multi-tenant routing     │
│   OpenAPI docs · Request validation · Versioning (/v1/)           │
└──┬──────────┬──────────┬──────────┬──────────┬──────────────────┘
   │          │          │          │          │
┌──▼──┐  ┌───▼──┐  ┌────▼──┐  ┌───▼──┐  ┌───▼──────────┐
│Mem  │  │Graph │  │Context│  │NLP   │  │User &        │
│Ingest│  │Mgr   │  │Assmbly│  │Enrich│  │Sessions      │
└──┬──┘  └───┬──┘  └────┬──┘  └───┬──┘  └───┬──────────┘
   └─────────┴──────────┴─────────┴──────────┘
                         │
              ┌──────────▼──────────┐
              │   ARQ Worker Pool    │
              │   (Redis-backed)     │
              │ Entity extraction    │
              │ Embedding jobs       │
              │ Community summaries  │
              │ Business data ingest │
              └──────────┬──────────┘
                         │
              ┌──────────▼──────────┐
              │   Graphiti Engine    │
              │  ┌────┐ ┌────┐ ┌────┐│
              │  │Epis│ │Sem │ │Temp││
              │  └────┘ └────┘ └────┘│
              │  ┌────────────────┐  │
              │  │Community layer │  │
              │  └────────────────┘  │
              └──────────┬──────────┘
                         │
              ┌──────────▼──────────┐
              │   Hybrid Retrieval   │
              │  pgvector (cosine)   │
              │  BM25 / pg_trgm      │
              │  Graph BFS traversal │
              │  RRF re-ranking      │
              └──────────┬──────────┘
                         │
         ┌───────────────┼───────────────┐
    ┌────▼───┐     ┌─────▼────┐    ┌────▼──┐
    │Postgres│     │FalkorDB  │    │Redis  │
    │+pgvect │     │(graph DB)│    │cache  │
    └────────┘     └──────────┘    └───────┘
```

### 4.2 Monorepo Structure

```
OpenZep/
├── services/
│   ├── api/                    # FastAPI gateway
│   │   ├── routers/            # Route handlers per domain
│   │   ├── dependencies/       # Auth, DB, rate-limiting DI
│   │   ├── middleware/         # Logging, tracing, error handling
│   │   └── main.py
│   ├── worker/                 # ARQ async workers
│   │   ├── tasks/              # entity_extraction, embedding, summarise
│   │   └── worker.py
│   └── mcp/                    # MCP server (stdio + SSE)
│       ├── tools/
│       └── server.py
├── packages/
│   ├── core/                   # Shared domain models, DB clients
│   ├── graphiti-client/        # Thin wrapper around graphiti library
│   ├── sdk-python/             # Published to PyPI
│   ├── sdk-typescript/         # Published to npm
│   └── sdk-go/                 # Published to pkg.go.dev
├── apps/
│   └── dashboard/              # Next.js 14 admin UI
├── infra/
│   ├── docker-compose.yml
│   ├── docker-compose.prod.yml
│   └── helm/                   # Kubernetes Helm chart
├── scripts/
│   └── migrate.py
├── tests/
│   ├── unit/
│   ├── integration/
│   └── e2e/
└── docs/
    └── openapi.yaml
```

---

## 5. Functional Requirements

Requirements are tagged with priority: **P0** (must-have MVP), **P1** (important, phase 2), **P2** (nice-to-have, phase 3).

---

### 5.1 Authentication & Multi-Tenancy

#### 5.1.1 API Key Authentication

| ID | Requirement | Priority |
|---|---|---|
| AUTH-01 | System shall support API key-based authentication on all `/v1/` endpoints | P0 |
| AUTH-02 | API keys shall be scoped to an `organization_id` (tenant) | P0 |
| AUTH-03 | API keys shall be prefixed `mg_live_` (production) and `mg_test_` (sandbox) | P0 |
| AUTH-04 | System shall support key rotation without downtime | P0 |
| AUTH-05 | System shall support JWT authentication for the admin dashboard | P1 |
| AUTH-06 | System shall support per-key rate limiting configurable per tenant | P1 |

#### 5.1.2 Multi-Tenancy

| ID | Requirement | Priority |
|---|---|---|
| MT-01 | All data (users, sessions, graph nodes) shall be namespaced by `organization_id` | P0 |
| MT-02 | Cross-tenant data access shall be impossible at the query layer | P0 |
| MT-03 | Each tenant shall have isolated graph namespaces in FalkorDB | P0 |
| MT-04 | Tenant quotas (users, API calls, graph nodes) shall be configurable | P1 |
| MT-05 | Admin super-keys shall allow cross-tenant management from the dashboard | P1 |

---

### 5.2 Memory Ingestion

#### 5.2.1 Message Ingestion

| ID | Requirement | Priority |
|---|---|---|
| ING-01 | System shall accept `POST /v1/users/{user_id}/memory` with a list of `Message` objects | P0 |
| ING-02 | Each `Message` shall have: `role` (user/assistant/system/tool), `content` (string), `created_at` (ISO-8601 timestamp) | P0 |
| ING-03 | Messages shall be stored as episodes in the Graphiti episodic layer | P0 |
| ING-04 | Ingestion shall return HTTP 202 (Accepted) and enqueue async enrichment | P0 |
| ING-05 | System shall support optional `session_id` for grouping messages into a conversation session | P0 |
| ING-06 | System shall support optional `metadata` dict on messages for caller-defined tags | P1 |

#### 5.2.2 Business Data Ingestion

| ID | Requirement | Priority |
|---|---|---|
| BIZ-01 | System shall accept `POST /v1/users/{user_id}/facts` to push structured business data | P1 |
| BIZ-02 | Business data shall be expressed as `(subject, predicate, object, valid_at, expires_at)` triples | P1 |
| BIZ-03 | System shall accept batch ingestion of up to 500 fact triples per request | P1 |
| BIZ-04 | System shall accept `POST /v1/users/{user_id}/documents` for unstructured business documents (emails, transcripts, support tickets) — processed by NLP pipeline | P2 |

---

### 5.3 Knowledge Graph Management

#### 5.3.1 Graphiti Integration

| ID | Requirement | Priority |
|---|---|---|
| KG-01 | System shall use Graphiti as the temporal KG engine | P0 |
| KG-02 | System shall support FalkorDB and Neo4j as pluggable graph backends | P0 |
| KG-03 | Graphiti shall be configured with OpenAI-compatible LLM and embedding backends | P0 |
| KG-04 | System shall support local Ollama as a drop-in LLM/embedding backend | P1 |

#### 5.3.2 Graph Layers

| ID | Requirement | Priority |
|---|---|---|
| KG-05 | **Episodic layer**: raw conversation sessions stored as episode nodes with `created_at` timestamps | P0 |
| KG-06 | **Semantic layer**: entities (Person, Company, Product, etc.) and typed relationships extracted from episodes | P0 |
| KG-07 | **Temporal layer**: facts stored with `valid_from`, `valid_to`, and `invalid_at` fields supporting bi-temporal modelling | P0 |
| KG-08 | **Community layer**: entity clusters summarised into community nodes for long-context compression | P1 |

#### 5.3.3 Graph Query API

| ID | Requirement | Priority |
|---|---|---|
| KG-09 | `GET /v1/users/{user_id}/graph/nodes` — list all entity nodes for a user with filtering by type and date | P0 |
| KG-10 | `GET /v1/users/{user_id}/graph/nodes/{node_id}` — get a single entity node with all edges | P0 |
| KG-11 | `GET /v1/users/{user_id}/graph/edges` — list relationships with optional subject/predicate filters | P1 |
| KG-12 | `DELETE /v1/users/{user_id}/graph/nodes/{node_id}` — delete a node and all its edges | P1 |
| KG-13 | `GET /v1/users/{user_id}/graph/communities` — list community summary nodes | P1 |

---

### 5.4 Context Assembly & Retrieval

#### 5.4.1 Context Endpoint

| ID | Requirement | Priority |
|---|---|---|
| CTX-01 | `GET /v1/users/{user_id}/context?query={text}&limit={n}` — return an assembled context block ready for LLM injection | P0 |
| CTX-02 | Context block shall include: relevant facts, entity summaries, recent episodic messages, community summaries | P0 |
| CTX-03 | Context endpoint p99 latency shall be ≤ 300ms with warm cache | P0 |
| CTX-04 | Context block shall be returned as a plain string by default, with optional `format=json` for structured output | P0 |
| CTX-05 | Context assembly shall use BFS graph traversal from the user node up to configurable depth (default: 2 hops) | P0 |
| CTX-06 | System shall cache assembled context blocks in Redis with configurable TTL (default: 30s) | P0 |

#### 5.4.2 Hybrid Retrieval Engine

| ID | Requirement | Priority |
|---|---|---|
| RET-01 | System shall perform **vector similarity search** using pgvector cosine distance on entity and episode embeddings | P0 |
| RET-02 | System shall perform **BM25 full-text search** using PostgreSQL `pg_trgm` or GIN indexes | P0 |
| RET-03 | System shall perform **graph BFS traversal** from user node with edge-weight scoring | P0 |
| RET-04 | Results from all three paths shall be merged with **Reciprocal Rank Fusion (RRF)** | P0 |
| RET-05 | System shall support a pluggable **cross-encoder re-ranker** as an optional post-processing step | P2 |
| RET-06 | `GET /v1/users/{user_id}/search?query={text}&types=facts,episodes,entities` — standalone search endpoint returning ranked results | P1 |

---

### 5.5 NLP Enrichment Pipeline

All enrichment runs asynchronously via the ARQ worker pool after ingestion.

#### 5.5.1 Entity Extraction

| ID | Requirement | Priority |
|---|---|---|
| NLP-01 | System shall extract named entities (Person, Organisation, Location, Product, Date, Custom) from messages using an LLM | P0 |
| NLP-02 | System shall extract typed relationships between entities as `(subject, predicate, object)` triples | P0 |
| NLP-03 | System shall resolve co-references across a session (e.g., "he" → previously mentioned person) | P1 |
| NLP-04 | Extraction prompts shall be configurable per organisation to support custom ontologies | P1 |

#### 5.5.2 Fact Extraction

| ID | Requirement | Priority |
|---|---|---|
| NLP-05 | System shall auto-extract factual statements from conversations without requiring a pre-defined schema (zero-shot) | P0 |
| NLP-06 | Extracted facts shall be stored in a `facts` table with `user_id`, `content`, `confidence`, `source_episode_id`, `valid_from`, `valid_to` | P0 |
| NLP-07 | `GET /v1/users/{user_id}/facts` — list all extracted facts with optional filter by date range and confidence threshold | P0 |

#### 5.5.3 Dialog Classification

| ID | Requirement | Priority |
|---|---|---|
| NLP-08 | System shall classify each session turn with **intent** (e.g., `question`, `complaint`, `purchase_intent`, `chitchat`) | P1 |
| NLP-09 | System shall classify each session turn with **emotion** (valence: positive/neutral/negative; arousal: low/high) | P1 |
| NLP-10 | Classification labels shall be configurable per organisation | P1 |
| NLP-11 | Classification results shall be queryable via `GET /v1/users/{user_id}/sessions/{session_id}/classifications` | P1 |

#### 5.5.4 Structured Data Extraction

| ID | Requirement | Priority |
|---|---|---|
| NLP-12 | Organisations shall be able to define a JSON Schema for structured data extraction from conversations | P1 |
| NLP-13 | System shall run extraction asynchronously after each session and store results in a `structured_extractions` table | P1 |
| NLP-14 | `GET /v1/users/{user_id}/sessions/{session_id}/extract` — return structured data extracted against the org's schema | P1 |

#### 5.5.5 Community Summarisation

| ID | Requirement | Priority |
|---|---|---|
| NLP-15 | System shall detect entity clusters (communities) using graph community detection algorithms (Louvain or Label Propagation) | P1 |
| NLP-16 | System shall generate a natural-language summary for each community via LLM | P1 |
| NLP-17 | Community summaries shall be regenerated on a configurable schedule (default: nightly) | P1 |

---

### 5.6 User & Session Management

#### 5.6.1 Users

| ID | Requirement | Priority |
|---|---|---|
| USR-01 | `POST /v1/users` — create a user with `user_id`, optional `name`, `email`, `metadata` | P0 |
| USR-02 | `GET /v1/users/{user_id}` — retrieve user profile and summary stats | P0 |
| USR-03 | `PATCH /v1/users/{user_id}` — update user metadata | P0 |
| USR-04 | `DELETE /v1/users/{user_id}` — delete user and all associated data (GDPR-compliant cascade) | P0 |
| USR-05 | `GET /v1/users` — list users with pagination, search by email/metadata, created_at range filter | P1 |

#### 5.6.2 Sessions

| ID | Requirement | Priority |
|---|---|---|
| SES-01 | `POST /v1/users/{user_id}/sessions` — create a named session | P0 |
| SES-02 | `GET /v1/users/{user_id}/sessions` — list all sessions with pagination | P0 |
| SES-03 | `GET /v1/users/{user_id}/sessions/{session_id}` — get session detail including message count, facts extracted | P0 |
| SES-04 | `GET /v1/users/{user_id}/sessions/{session_id}/messages` — paginated message history | P0 |
| SES-05 | `DELETE /v1/users/{user_id}/sessions/{session_id}` — delete session and unlink from graph | P1 |

---

### 5.7 Async Worker System

| ID | Requirement | Priority |
|---|---|---|
| WRK-01 | All NLP enrichment tasks shall run asynchronously via ARQ workers | P0 |
| WRK-02 | Worker tasks shall be idempotent — re-queuing a task for an already-processed episode shall be a no-op | P0 |
| WRK-03 | Workers shall implement exponential backoff on LLM API failures (max 3 retries) | P0 |
| WRK-04 | Worker queue depth and task latency shall be exposed as Prometheus metrics | P0 |
| WRK-05 | System shall support horizontal scaling of workers independently of the API layer | P0 |
| WRK-06 | Dead-letter queue shall capture permanently failed tasks for inspection | P1 |
| WRK-07 | Workers shall support priority queues: `high` (real-time ingestion) and `low` (community summarisation batch) | P1 |

**Worker task types:**

| Task | Trigger | Queue |
|---|---|---|
| `extract_entities` | After message ingestion | high |
| `embed_episode` | After entity extraction | high |
| `embed_entity` | After entity upsert | high |
| `extract_facts` | After message ingestion | high |
| `classify_dialog` | After session turn | high |
| `extract_structured` | After session close | high |
| `summarise_community` | Scheduled nightly | low |
| `ingest_business_data` | After business data POST | low |

---

### 5.8 SDKs

#### 5.8.1 Python SDK (`OpenZep-py`)

| ID | Requirement | Priority |
|---|---|---|
| SDK-01 | SDK shall support both sync and async (asyncio) interfaces | P0 |
| SDK-02 | SDK shall be published to PyPI as `OpenZep-py` | P0 |
| SDK-03 | SDK shall implement automatic retry with exponential backoff | P0 |
| SDK-04 | SDK shall support environment variable configuration (`MEMGRAPH_API_KEY`, `MEMGRAPH_BASE_URL`) | P0 |

```python
# Target developer experience
from OpenZep import OpenZep

client = OpenZep(api_key="mg_live_...", base_url="http://localhost:8000")

# Add memory
await client.memory.add(
    user_id="user_123",
    session_id="session_abc",
    messages=[
        {"role": "user", "content": "I prefer Python over JavaScript."},
        {"role": "assistant", "content": "Noted! I'll keep that in mind."}
    ]
)

# Retrieve context
context = await client.memory.get(user_id="user_123", query="programming preferences")
# Returns: "User prefers Python. Last mentioned: 2026-06-03."

# Search graph
nodes = await client.graph.search(user_id="user_123", query="preferences", types=["facts"])

# Add business data
await client.facts.add(user_id="user_123", facts=[
    {"subject": "user_123", "predicate": "purchased", "object": "Pro plan", "valid_at": "2026-05-01"}
])
```

#### 5.8.2 TypeScript SDK (`OpenZep-ts`)

| ID | Requirement | Priority |
|---|---|---|
| SDK-05 | SDK shall be published to npm as `OpenZep-ts` | P1 |
| SDK-06 | SDK shall be fully typed with TypeScript generics on structured extraction responses | P1 |
| SDK-07 | SDK shall support both Node.js (18+) and browser (via fetch) | P1 |

#### 5.8.3 Go SDK (`OpenZep-go`)

| ID | Requirement | Priority |
|---|---|---|
| SDK-08 | SDK shall be published to `pkg.go.dev` as `github.com/thelinkAI/OpenZep-go` | P2 |
| SDK-09 | SDK shall use idiomatic Go patterns (context propagation, error returns) | P2 |

---

### 5.9 MCP Server

| ID | Requirement | Priority |
|---|---|---|
| MCP-01 | System shall expose an MCP server supporting stdio and SSE transports | P0 |
| MCP-02 | MCP server shall implement the following tools: | P0 |

| Tool | Description |
|---|---|
| `add_memory` | Add messages to a user's memory |
| `get_context` | Retrieve assembled context block for a query |
| `search_memory` | Search memory across facts, episodes, and entities |
| `add_fact` | Manually assert a fact triple |
| `list_facts` | List extracted facts for a user |
| `get_user_graph` | Return entity nodes and edges for a user |
| `create_user` | Create a new user record |
| `list_sessions` | List conversation sessions for a user |

| ID | Requirement | Priority |
|---|---|---|
| MCP-03 | MCP server shall authenticate via the same API key mechanism as the REST API | P0 |
| MCP-04 | MCP server shall be configurable as a Claude Desktop / Cursor MCP provider via `mcpServers` config | P0 |

---

### 5.10 Admin Dashboard

| ID | Requirement | Priority |
|---|---|---|
| DASH-01 | Dashboard shall be a Next.js 14 app served at `/dashboard` | P1 |
| DASH-02 | **Tenant management**: create/delete organisations, generate/revoke API keys, set quotas | P1 |
| DASH-03 | **User management**: list users, view user graph, delete user data | P1 |
| DASH-04 | **Graph explorer**: interactive visualisation of entity nodes and edges per user | P1 |
| DASH-05 | **Memory search**: search a user's memory by natural language query with highlighted results | P1 |
| DASH-06 | **Usage analytics**: API call counts, token usage, graph node counts, active users — pulling from existing Grafana/Mimir | P1 |
| DASH-07 | **Job monitor**: view ARQ worker queue depth, task success/failure rates, dead-letter queue | P2 |
| DASH-08 | Dashboard authentication shall use JWT with short-lived tokens (15-minute expiry) and refresh tokens | P1 |

---

## 6. Non-Functional Requirements

### 6.1 Performance

| ID | Requirement | Target |
|---|---|---|
| PERF-01 | `GET /context` p50 latency (warm cache) | ≤ 50ms |
| PERF-02 | `GET /context` p99 latency (warm cache) | ≤ 300ms |
| PERF-03 | `GET /context` p99 latency (cold, full hybrid retrieval) | ≤ 1500ms |
| PERF-04 | `POST /memory` ingestion acknowledgement | ≤ 200ms (async 202) |
| PERF-05 | Entity extraction worker task completion | ≤ 30s from ingestion |
| PERF-06 | API gateway throughput (single node) | ≥ 500 req/s |

### 6.2 Scalability

| ID | Requirement |
|---|---|
| SCALE-01 | API and worker layers shall scale horizontally independently |
| SCALE-02 | System shall support ≥ 1M users per tenant without schema changes |
| SCALE-03 | Embedding jobs shall support batching (default: 100 texts per OpenAI call) |
| SCALE-04 | Graph queries shall remain sub-second for users with ≤ 10,000 entity nodes |

### 6.3 Availability

| ID | Requirement |
|---|---|
| AVAIL-01 | API availability target: 99.9% uptime (Kubernetes deployment) |
| AVAIL-02 | Rolling deploys shall have zero downtime |
| AVAIL-03 | Database connection pool shall handle transient failures with automatic reconnection |

### 6.4 Maintainability

| ID | Requirement |
|---|---|
| MAINT-01 | Unit test coverage ≥ 80% on all service packages |
| MAINT-02 | Integration tests shall cover all P0 API endpoints |
| MAINT-03 | All public API endpoints shall be documented in OpenAPI 3.1 spec |
| MAINT-04 | Database migrations shall use Alembic with versioned migration files |
| MAINT-05 | All configuration via environment variables with sensible defaults |

### 6.5 Portability

| ID | Requirement |
|---|---|
| PORT-01 | Entire platform shall run locally via `docker compose up` |
| PORT-02 | Graph backend shall be swappable via config (FalkorDB or Neo4j) without code changes |
| PORT-03 | LLM backend shall be swappable via config (OpenAI, Azure OpenAI, Ollama) |
| PORT-04 | Embedding backend shall be swappable via config |

---

## 7. Data Models

### 7.1 Core PostgreSQL Tables

#### `organizations`
```sql
CREATE TABLE organizations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    plan        TEXT NOT NULL DEFAULT 'free',
    quotas      JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

#### `api_keys`
```sql
CREATE TABLE api_keys (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    key_hash        TEXT NOT NULL UNIQUE,   -- bcrypt hash of raw key
    prefix          TEXT NOT NULL,          -- mg_live_ or mg_test_
    name            TEXT,
    scopes          TEXT[] DEFAULT ARRAY['read','write'],
    last_used_at    TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

#### `users`
```sql
CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    external_id     TEXT NOT NULL,          -- caller-defined user_id
    name            TEXT,
    email           TEXT,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, external_id)
);
```

#### `sessions`
```sql
CREATE TABLE sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    external_id     TEXT NOT NULL,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at       TIMESTAMPTZ,
    UNIQUE (user_id, external_id)
);
```

#### `episodes`
```sql
CREATE TABLE episodes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user','assistant','system','tool')),
    content         TEXT NOT NULL,
    metadata        JSONB DEFAULT '{}',
    embedding       VECTOR(1536),           -- pgvector
    graphiti_node_id TEXT,                  -- reference in graph DB
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ON episodes USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX ON episodes USING GIN (to_tsvector('english', content));
```

#### `facts`
```sql
CREATE TABLE facts (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content           TEXT NOT NULL,
    subject           TEXT,
    predicate         TEXT,
    object            TEXT,
    confidence        FLOAT4 DEFAULT 1.0,
    source_episode_id UUID REFERENCES episodes(id),
    valid_from        TIMESTAMPTZ,
    valid_to          TIMESTAMPTZ,
    invalid_at        TIMESTAMPTZ,
    embedding         VECTOR(1536),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ON facts USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
```

#### `structured_extractions`
```sql
CREATE TABLE structured_extractions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    schema_id   UUID,
    data        JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

#### `dialog_classifications`
```sql
CREATE TABLE dialog_classifications (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    episode_id  UUID NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    intent      TEXT,
    emotion     TEXT,
    valence     TEXT CHECK (valence IN ('positive','neutral','negative')),
    arousal     TEXT CHECK (arousal IN ('low','high')),
    raw         JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 7.2 Graphiti Graph Schema (FalkorDB / Neo4j)

**Node types:**

| Label | Key Properties |
|---|---|
| `EntityNode` | `uuid`, `name`, `type`, `summary`, `created_at`, `org_id` |
| `EpisodicNode` | `uuid`, `content`, `source`, `source_id`, `created_at`, `org_id` |
| `CommunityNode` | `uuid`, `name`, `summary`, `created_at`, `org_id` |

**Relationship types:**

| Type | Properties |
|---|---|
| `RELATES_TO` | `fact`, `fact_embedding`, `episodes`, `valid_at`, `invalid_at`, `created_at` |
| `HAS_EPISODE` | `created_at` |
| `MEMBER_OF` | `created_at` |

---

## 8. API Specification

### 8.1 Base URL

```
https://your-host/v1
```

### 8.2 Authentication

All requests must include:

```
Authorization: Bearer mg_live_<key>
```

### 8.3 Endpoint Summary

#### Memory

| Method | Path | Description |
|---|---|---|
| `POST` | `/users/{user_id}/memory` | Ingest messages into memory |
| `GET` | `/users/{user_id}/context` | Get assembled LLM context block |
| `DELETE` | `/users/{user_id}/memory` | Wipe all memory for a user |

#### Facts

| Method | Path | Description |
|---|---|---|
| `POST` | `/users/{user_id}/facts` | Add fact triples manually |
| `GET` | `/users/{user_id}/facts` | List all facts |
| `DELETE` | `/users/{user_id}/facts/{fact_id}` | Delete a fact |

#### Graph

| Method | Path | Description |
|---|---|---|
| `GET` | `/users/{user_id}/graph/nodes` | List entity nodes |
| `GET` | `/users/{user_id}/graph/nodes/{node_id}` | Get node + edges |
| `DELETE` | `/users/{user_id}/graph/nodes/{node_id}` | Delete node |
| `GET` | `/users/{user_id}/graph/edges` | List relationships |
| `GET` | `/users/{user_id}/graph/communities` | List communities |

#### Search

| Method | Path | Description |
|---|---|---|
| `GET` | `/users/{user_id}/search` | Hybrid search across memory |

#### Users

| Method | Path | Description |
|---|---|---|
| `POST` | `/users` | Create user |
| `GET` | `/users` | List users (paginated) |
| `GET` | `/users/{user_id}` | Get user |
| `PATCH` | `/users/{user_id}` | Update user metadata |
| `DELETE` | `/users/{user_id}` | Delete user + all data |

#### Sessions

| Method | Path | Description |
|---|---|---|
| `POST` | `/users/{user_id}/sessions` | Create session |
| `GET` | `/users/{user_id}/sessions` | List sessions |
| `GET` | `/users/{user_id}/sessions/{session_id}` | Get session |
| `GET` | `/users/{user_id}/sessions/{session_id}/messages` | Get messages |
| `DELETE` | `/users/{user_id}/sessions/{session_id}` | Delete session |

#### NLP (P1)

| Method | Path | Description |
|---|---|---|
| `GET` | `/users/{user_id}/sessions/{session_id}/classifications` | Get dialog classifications |
| `GET` | `/users/{user_id}/sessions/{session_id}/extract` | Get structured extraction |

#### Admin

| Method | Path | Description |
|---|---|---|
| `POST` | `/admin/organizations` | Create organisation |
| `GET` | `/admin/organizations` | List organisations |
| `POST` | `/admin/organizations/{org_id}/keys` | Generate API key |
| `DELETE` | `/admin/organizations/{org_id}/keys/{key_id}` | Revoke API key |

### 8.4 Error Response Format

```json
{
  "error": {
    "code": "RESOURCE_NOT_FOUND",
    "message": "User user_123 not found in organization org_abc",
    "request_id": "req_01j9xmf..."
  }
}
```

### 8.5 Pagination

All list endpoints support:
- `?limit=50` (default 50, max 200)
- `?cursor=<opaque_string>` (cursor-based pagination for consistency)

Response:
```json
{
  "data": [...],
  "next_cursor": "c_abc123",
  "has_more": true,
  "total": 1042
}
```

---

## 9. Technology Stack

### 9.1 Backend

| Component | Technology | Rationale |
|---|---|---|
| API framework | FastAPI + Python 3.11 | Team standard, async-native |
| ORM | async SQLAlchemy 2.0 + Alembic | Team standard |
| Background jobs | ARQ + Redis | Already adopted at TheLinkAI |
| Graph engine | Graphiti (Apache 2.0) | Best-in-class temporal KG, open source |
| Graph database | FalkorDB (primary) / Neo4j (alt) | FalkorDB: Redis-protocol, Redis-compatible infra; Neo4j: enterprise fallback |
| Vector store | pgvector (PostgreSQL extension) | Collocated with relational data, no extra service |
| Full-text search | PostgreSQL GIN + `pg_trgm` | Avoids separate Elasticsearch |
| Cache | Redis 7+ | Already in stack |
| LLM backend | OpenAI / Azure OpenAI / Ollama | Pluggable via config |

### 9.2 Frontend

| Component | Technology |
|---|---|
| Dashboard | Next.js 14 (App Router) |
| UI components | shadcn/ui + Tailwind CSS |
| Graph visualisation | Cytoscape.js or D3-force |
| API client | Auto-generated from OpenAPI spec |

### 9.3 Infrastructure

| Component | Technology |
|---|---|
| Containerisation | Docker + Docker Compose |
| Orchestration | Kubernetes + Helm chart |
| CI/CD | GitLab CI (self-hosted, TheLinkAI standard) |
| Observability | LGTM stack (Loki, Grafana, Tempo, Mimir, Alloy) — already deployed |
| Service mesh | Optional: Traefik ingress |

---

## 10. Infrastructure & Deployment

### 10.1 Docker Compose (Development / Single Node)

```yaml
services:
  api:        # FastAPI — port 8000
  worker:     # ARQ workers (2 replicas)
  postgres:   # PostgreSQL 15 + pgvector
  falkordb:   # FalkorDB (Redis-protocol, port 6380)
  redis:      # Redis 7 (port 6379) — queue + cache
  dashboard:  # Next.js — port 3000
  alloy:      # Grafana Alloy — metrics + logs collection
```

### 10.2 Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | — | PostgreSQL DSN |
| `GRAPH_BACKEND` | `falkordb` | `falkordb` or `neo4j` |
| `FALKORDB_URL` | `redis://localhost:6380` | FalkorDB connection |
| `REDIS_URL` | `redis://localhost:6379` | Redis connection |
| `LLM_BACKEND` | `openai` | `openai`, `azure`, `ollama` |
| `OPENAI_API_KEY` | — | Required for `openai` / `azure` backend |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Required for `ollama` backend |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model identifier |
| `EMBEDDING_DIM` | `1536` | Embedding vector dimensions |
| `CONTEXT_CACHE_TTL` | `30` | Context block Redis TTL in seconds |
| `MAX_WORKERS` | `4` | ARQ worker concurrency |
| `LOG_LEVEL` | `INFO` | Logging level |
| `SECRET_KEY` | — | JWT signing secret |

### 10.3 Migration Strategy

```bash
# Initial setup
alembic upgrade head

# New migration
alembic revision --autogenerate -m "add structured_extractions"
alembic upgrade head
```

---

## 11. Observability

Using TheLinkAI's existing LGTM stack (Loki, Grafana, Tempo, Mimir, Alloy).

### 11.1 Metrics (Prometheus → Mimir)

| Metric | Type | Description |
|---|---|---|
| `openzep_http_requests_total` | Counter | Requests by method, path, status |
| `openzep_http_request_duration_seconds` | Histogram | Latency by endpoint |
| `openzep_context_assembly_duration_seconds` | Histogram | Context retrieval latency |
| `openzep_worker_tasks_total` | Counter | Tasks by type, status (success/failure) |
| `openzep_worker_queue_depth` | Gauge | ARQ queue depth by queue name |
| `openzep_graph_nodes_total` | Gauge | Entity nodes per organisation |
| `openzep_embedding_tokens_total` | Counter | Tokens consumed for embeddings |
| `openzep_llm_tokens_total` | Counter | Tokens consumed for LLM calls |

### 11.2 Traces (Tempo)

- All inbound HTTP requests traced with OpenTelemetry
- Trace context propagated into ARQ worker tasks
- Graphiti graph operations traced as child spans
- LLM API calls traced with model and token count attributes

### 11.3 Logs (Loki)

- Structured JSON logs via `structlog`
- All logs include: `trace_id`, `org_id`, `user_id`, `task_type`
- Error logs include full exception context

### 11.4 Dashboards (Grafana)

Pre-built dashboard panels:

- API request rate and error rate (RED method)
- Context retrieval latency percentiles (p50/p95/p99)
- Worker queue depth and task throughput
- Token usage by organisation and model
- Graph node growth over time

---

## 12. Security Requirements

| ID | Requirement |
|---|---|
| SEC-01 | API keys shall be stored as bcrypt hashes; raw keys are shown only once at creation |
| SEC-02 | All inter-service communication shall use TLS in production |
| SEC-03 | Tenant isolation shall be enforced at the SQLAlchemy query layer using `organization_id` filter on every query |
| SEC-04 | User deletion (`DELETE /users/{user_id}`) shall cascade to all tables and delete graph nodes from FalkorDB |
| SEC-05 | SQL queries shall use parameterised statements; no string interpolation into queries |
| SEC-06 | Rate limiting shall prevent brute-force key enumeration (max 10 failed auth attempts per IP per minute) |
| SEC-07 | LLM API keys shall be stored in environment variables, never in database or logs |
| SEC-08 | Dashboard JWT tokens shall have 15-minute expiry; refresh tokens 7-day expiry, rotated on use |
| SEC-09 | Input validation shall reject messages over 64KB in content length |
| SEC-10 | Graph queries shall be parameterised with Graphiti's query API; no raw Cypher/GQL string construction |

---

## 13. Competitive Feature Parity Matrix

| Feature | Zep CE (self-hosted) | Zep Cloud (paid) | **OpenZep** |
|---|---|---|---|
| Temporal knowledge graph | ✅ Graphiti | ✅ Graphiti | ✅ Graphiti |
| Episodic memory | ✅ | ✅ | ✅ |
| Semantic entity extraction | ✅ | ✅ | ✅ |
| Bi-temporal facts | ✅ | ✅ | ✅ |
| Community summaries | ✅ | ✅ | ✅ P1 |
| Hybrid retrieval (vector + BM25 + graph) | ✅ | ✅ | ✅ |
| Context assembly endpoint | ✅ | ✅ | ✅ |
| Python SDK | ✅ | ✅ | ✅ |
| TypeScript SDK | ✅ | ✅ | ✅ P1 |
| Go SDK | ✅ | ✅ | ✅ P2 |
| MCP server | ✅ | ✅ | ✅ |
| Multi-tenancy | ❌ CE limited | ✅ | ✅ |
| Fact extraction (zero-shot) | ❌ | ✅ 💰 | ✅ Open |
| Dialog classification | ❌ | ✅ 💰 | ✅ P1 Open |
| Structured extraction | ❌ | ✅ 💰 | ✅ P1 Open |
| Business data ingestion | ❌ | ✅ | ✅ P1 |
| Admin dashboard | ❌ | ✅ | ✅ P1 |
| LGTM observability | ❌ | ❌ | ✅ native |
| Local LLM (Ollama) | ❌ | ❌ | ✅ |
| Air-gapped deployment | ❌ | ❌ | ✅ |
| Custom entity ontologies | Partial | Partial | ✅ P1 |
| Fully self-hosted | ✅ CE limited | ❌ | ✅ Full |

---

## 14. Build Phases & Milestones

### Phase 0 — Foundation (Week 1–2)

- [ ] Monorepo scaffold with `services/api`, `services/worker`, `packages/core`
- [ ] PostgreSQL schema + Alembic baseline migration
- [ ] FastAPI skeleton with auth middleware and multi-tenant routing
- [ ] Graphiti integration with FalkorDB backend
- [ ] Docker Compose dev environment

**Exit criteria:** `docker compose up` brings up a working API with auth, Graphiti connected to FalkorDB, and a passing health check.

### Phase 1 — Core Memory (Week 3–4)

- [ ] Message ingestion endpoint (`POST /memory`) with ARQ worker dispatch
- [ ] Entity extraction worker task
- [ ] Embedding worker task (episode + entity)
- [ ] Context assembly endpoint (`GET /context`) with hybrid retrieval
- [ ] Fact extraction worker task
- [ ] User and session CRUD endpoints

**Exit criteria:** A Python script can ingest a 10-turn conversation and retrieve a coherent context block under 300ms.

### Phase 2 — Full Feature Parity (Week 5–7)

- [ ] Business data ingestion (`POST /facts`)
- [ ] Community summarisation worker + schedule
- [ ] Python SDK (`OpenZep-py`) published to PyPI
- [ ] MCP server (stdio + SSE) with all 8 tools
- [ ] Graph query endpoints (nodes, edges, communities)
- [ ] Full-text search via `pg_trgm`

**Exit criteria:** SDK integration tests pass. MCP server works in Claude Desktop.

### Phase 3 — NLP Enrichment (Week 8–9)

- [ ] Dialog classification pipeline (intent + emotion)
- [ ] Structured extraction with org-defined JSON Schema
- [ ] Classification query endpoints
- [ ] Custom entity ontology support in extraction prompts

**Exit criteria:** Classification and structured extraction return consistent results on test conversations.

### Phase 4 — Dashboard & SDKs (Week 10–12)

- [ ] Next.js admin dashboard (tenant mgmt, user list, graph explorer)
- [ ] TypeScript SDK (`OpenZep-ts`) published to npm
- [ ] Grafana dashboards for API + worker metrics
- [ ] Kubernetes Helm chart with horizontal pod autoscaling
- [ ] Comprehensive OpenAPI 3.1 spec

**Exit criteria:** Dashboard is usable by a non-engineer for tenant management. Helm chart deploys cleanly on a 3-node cluster.

### Phase 5 — Hardening (Week 13–14)

- [ ] Unit test coverage ≥ 80%
- [ ] Integration test suite for all P0 endpoints
- [ ] Load test: 500 req/s sustained for 5 minutes with p99 < 300ms
- [ ] Security audit: SQL injection, auth bypass, cross-tenant access
- [ ] Go SDK scaffold
- [ ] CHANGELOG, contribution guide, LICENSE (Apache 2.0)

---

## 15. Open Questions & Risks

| ID | Question / Risk | Impact | Mitigation |
|---|---|---|---|
| OQ-01 | Graphiti API stability — library is relatively new, interfaces may change | High | Pin to a specific Graphiti version; abstract behind `packages/graphiti-client` |
| OQ-02 | FalkorDB maturity for production workloads | Medium | Keep Neo4j as a first-class alternative backend; integration-test both |
| OQ-03 | LLM cost at scale — extraction runs an LLM call per ingestion | High | Implement cost controls: batch processing, configurable enrichment depth per org, budget alerts |
| OQ-04 | pgvector performance at > 10M vectors | Medium | Evaluate migrating to Qdrant as an optional vector backend in Phase 5 |
| OQ-05 | Graph query latency for users with large graphs (> 50k nodes) | Medium | Add depth limit on BFS traversal; implement graph pruning strategy |
| OQ-06 | SDK versioning — keeping Python, TS, Go SDKs in sync with API | Medium | Generate SDK clients from OpenAPI spec where possible |
| OQ-07 | Community detection algorithm performance on large graphs | Low | Run community detection only on orgs above a size threshold; make it opt-in |

---

## 16. Glossary

| Term | Definition |
|---|---|
| **ARQ** | Async Redis Queue — Python background job library backed by Redis, used as TheLinkAI's standard worker system |
| **BFS** | Breadth-First Search — graph traversal algorithm used to explore entity relationships |
| **Bi-temporal** | A data model tracking two time axes: *valid time* (when a fact is true in the real world) and *transaction time* (when the fact was recorded in the system) |
| **BM25** | Best Match 25 — probabilistic full-text ranking algorithm used alongside vector search |
| **Community** | A cluster of related entities in the knowledge graph, summarised into a natural-language description |
| **Context block** | An assembled string containing relevant facts, entity summaries, and recent episodes, formatted for injection into an LLM prompt |
| **Episode** | A single raw conversation message stored in the episodic memory layer |
| **Fact triple** | A `(subject, predicate, object)` statement, e.g., `(user_123, purchased, Pro plan)` |
| **FalkorDB** | An open-source graph database using the Redis wire protocol, supporting the property graph model |
| **Graphiti** | Open-source temporal knowledge graph engine by Zep (Apache 2.0), used as OpenZep's core graph layer |
| **Hybrid retrieval** | Combining vector similarity search, BM25 full-text, and graph traversal, merged with RRF |
| **LGTM** | Loki + Grafana + Tempo + Mimir — TheLinkAI's self-hosted observability stack |
| **MCP** | Model Context Protocol — an open protocol for exposing tools to LLM agents |
| **Multi-tenancy** | Serving multiple organisations from a single deployment with strict data isolation |
| **NER** | Named Entity Recognition — identifying entities (people, places, organisations) in text |
| **Ontology** | A formal schema defining entity types and relationship types in the knowledge graph |
| **pgvector** | PostgreSQL extension for storing and querying vector embeddings |
| **RRF** | Reciprocal Rank Fusion — algorithm for merging ranked result lists from multiple retrieval methods |
| **TKG** | Temporal Knowledge Graph — a knowledge graph where facts carry validity timestamps |
| **Valid time** | The real-world time period during which a fact is true |
| **Transaction time** | The system time at which a fact was recorded |

---

*Document maintained by Rohan · TheLinkAI · Last updated: 2026-06-05*
