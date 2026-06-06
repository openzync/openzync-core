# OpenZep — Implementation Documentation Suite

> **Purpose:** These documents translate the SRS (`SRS_MemGraph.md`) into precise, actionable implementation guides for every domain. Each document follows a consistent template: Overview → Data Model → API Contracts → Service Layer → Repository Layer → Worker Integration → Sequence Diagram → Testing Guide → Configuration → Open Questions.
>
> **Audience:** All engineers building OpenZep. Read the domain docs relevant to your current phase before writing code.
>
> **Dependency Rule:** Documents are numbered in dependency order. Read `01-data-models/` before anything else. Within a domain, read docs in filename order.

---

## Document Dependency Graph

```
01-data-models/ (Foundation — read first)
   │
   ▼
02-auth-tenancy/ (Cross-cutting — every endpoint depends on this)
   │
   ├──► 03-core-memory/ (Primary value proposition)
   │       │
   │       ├──► 04-knowledge-graph/ (Graphiti integration)
   │       │
   │       ├──► 05-nlp-pipeline/ (Async enrichment)
   │       │       │
   │       │       └──► 06-worker-system/ (ARQ infrastructure)
   │       │
   │       └──► 07-user-session-mgmt/ (CRUD foundation)
   │
   ├──► 08-api-gateway/ (FastAPI infrastructure)
   │
   ├──► 09-sdks/ (Client libraries — published separately)
   │
   ├──► 10-mcp-server/ (MCP protocol)
   │
   ├──► 11-dashboard/ (Next.js admin UI)
   │
   ├──► 12-observability/ (LGTM stack)
   │
   ├──► 13-deployment/ (Infrastructure)
   │
   └──► 14-testing/ (Quality assurance — reference during every phase)
```

---

## Wave 1 — Foundation (Critical Path)

Build these first. Every other domain depends on them.

| # | Document | Key Contents |
|---|----------|-------------|
| 1.1 | [01-postgresql-schema.md](01-data-models/01-postgresql-schema.md) | Complete DDL for all 7+ tables, all 18+ indexes, CHECK constraints, migration strategy |
| 1.2 | [02-graphiti-schema.md](01-data-models/02-graphiti-schema.md) | Graphiti node/edge types, properties, temporal query patterns |
| 1.3 | [03-embedding-strategy.md](01-data-models/03-embedding-strategy.md) | pgvector config, dimension management, IVFFlat vs HNSW, re-indexing schedule |
| 2.1 | [01-api-key-auth.md](02-auth-tenancy/01-api-key-auth.md) | API key lifecycle: generate, hash, validate, rotate, revoke |
| 2.2 | [02-jwt-auth.md](02-auth-tenancy/02-jwt-auth.md) | Dashboard JWT tokens, refresh rotation, expiry |
| 2.3 | [03-tenant-isolation.md](02-auth-tenancy/03-tenant-isolation.md) | PostgreSQL RLS + SQLAlchemy filters + FalkorDB namespaces |
| 2.4 | [04-rate-limiting.md](02-auth-tenancy/04-rate-limiting.md) | Per-key and per-IP rate limiting, Redis-backed, configurable |

---

## Wave 2 — Core Features

The heart of the system: memory ingestion, context assembly, and knowledge graph.

| # | Document | Key Contents |
|---|----------|-------------|
| 3.1 | [01-message-ingestion.md](03-core-memory/01-message-ingestion.md) | `POST /memory`: router → service → repo → ARQ enqueue. Dual-write resolution. Idempotency. |
| 3.2 | [02-context-assembly.md](03-core-memory/02-context-assembly.md) | `GET /context`: BFS + vector + BM25 + RRF pipeline. Latency budget analysis. |
| 3.3 | [03-hybrid-retrieval.md](03-core-memory/03-hybrid-retrieval.md) | pgvector cosine, pg_trgm BM25, graph BFS, RRF merge, cross-encoder re-rank |
| 3.4 | [04-caching-strategy.md](03-core-memory/04-caching-strategy.md) | Redis TTL, invalidation on new ingestion, cache-aside pattern, hit-rate monitoring |
| 3.5 | [05-idempotency-dedup.md](03-core-memory/05-idempotency-dedup.md) | HTTP idempotency-key header, content-hash dedup, worker idempotency |
| 4.1 | [01-graphiti-setup.md](04-knowledge-graph/01-graphiti-setup.md) | Library init, FalkorDB/Neo4j connection, version pinning, async compatibility |
| 4.2 | [02-entity-operations.md](04-knowledge-graph/02-entity-operations.md) | Entity/edge CRUD, graph query endpoints, pagination, error handling |
| 4.3 | [03-temporal-queries.md](04-knowledge-graph/03-temporal-queries.md) | Valid-time queries, bi-temporal patterns, fact versioning |
| 4.4 | [04-community-detection.md](04-knowledge-graph/04-community-detection.md) | Louvain/Label Propagation, LLM summarisation worker, schedule |
| 4.5 | [05-graph-client-abstraction.md](04-knowledge-graph/05-graph-client-abstraction.md) | Backend-agnostic wrapper: FalkorDB ↔ Neo4j, org_id enforcement |
| 4.6 | [06-postgres-graph-backend.md](04-knowledge-graph/06-postgres-graph-backend.md) | PostgreSQL-native graph backend replacing Graphiti, recursive CTE BFS, migration plan |

---

## Wave 3 — NLP Pipeline & Worker System

Async enrichment and the ARQ infrastructure powering it.

| # | Document | Key Contents |
|---|----------|-------------|
| 5.1 | [01-prompt-templates.md](05-nlp-pipeline/01-prompt-templates.md) | All prompt structures, anti-injection guardrails, versioning strategy |
| 5.2 | [02-entity-extraction.md](05-nlp-pipeline/02-entity-extraction.md) | Worker task: LLM call → parse → persist. Prompt, retry, eval. |
| 5.3 | [03-fact-extraction.md](05-nlp-pipeline/03-fact-extraction.md) | Worker task: zero-shot fact extraction from conversation turns |
| 5.4 | [04-dialog-classification.md](05-nlp-pipeline/04-dialog-classification.md) | Intent + emotion classification worker, configurable labels |
| 5.5 | [05-structured-extraction.md](05-nlp-pipeline/05-structured-extraction.md) | JSON Schema-based extraction, schema CRUD, worker |
| 5.6 | [06-pii-detection.md](05-nlp-pipeline/06-pii-detection.md) | Pre-LLM PII redaction, regex/Spacy, configurable per org |
| 5.7 | [07-llm-cost-control.md](05-nlp-pipeline/07-llm-cost-control.md) | Per-org budgets, enrichment depth levels, Ollama default, alerting |
| 6.1 | [01-arq-setup.md](06-worker-system/01-arq-setup.md) | Queue config, Redis connection, worker process, health checks |
| 6.2 | [02-task-definitions.md](06-worker-system/02-task-definitions.md) | All 8+ task types with input/output schemas, queue assignment |
| 6.3 | [03-task-orchestration.md](06-worker-system/03-task-orchestration.md) | DAG chaining: orchestrator pattern vs event-driven, dependency resolution |
| 6.4 | [04-retry-backoff-dlq.md](06-worker-system/04-retry-backoff-dlq.md) | Exponential backoff formula, max retries, DLQ inspection/re-queue |
| 6.5 | [05-priority-queues.md](06-worker-system/05-priority-queues.md) | High/Low queue management, worker pool allocation, starvation prevention |
| 6.6 | [06-scheduled-tasks.md](06-worker-system/06-scheduled-tasks.md) | Community summarisation cron, data retention cleanup, cache warming |

---

## Wave 4 — API, SDKs & User Management

The developer-facing surface area.

| # | Document | Key Contents |
|---|----------|-------------|
| 7.1 | [01-user-crud.md](07-user-session-mgmt/01-user-crud.md) | User create/read/update/delete/list with pagination, search, metadata |
| 7.2 | [02-session-crud.md](07-user-session-mgmt/02-session-crud.md) | Session create/list/get/messages/delete, auto-close, session grouping |
| 7.3 | [03-gdpr-compliance.md](07-user-session-mgmt/03-gdpr-compliance.md) | Cascade delete across all stores, data portability export, retention policy |
| 8.1 | [01-app-setup.md](08-api-gateway/01-app-setup.md) | FastAPI lifespan, middleware registration, CORS, global exception handlers |
| 8.2 | [02-error-handling.md](08-api-gateway/02-error-handling.md) | Exception hierarchy, RFC 7807 problem details, error code catalogue |
| 8.3 | [03-pagination.md](08-api-gateway/03-pagination.md) | Cursor-based pagination reference implementation, encoding, performance |
| 8.4 | [04-request-validation.md](08-api-gateway/04-request-validation.md) | Pydantic schemas per domain, 64KB limit, input sanitisation |
| 8.5 | [05-openapi-generation.md](08-api-gateway/05-openapi-generation.md) | Auto-generated OpenAPI 3.1, versioning, SDK generation from spec |
| 9.1 | [01-shared-patterns.md](09-sdks/01-shared-patterns.md) | Retry, auth, error handling, pagination patterns shared across all SDKs |
| 9.2 | [02-python-sdk.md](09-sdks/02-python-sdk.md) | Sync/async duality, PyPI packaging, full API reference, all CRUD operations |
| 9.3 | ~~[03-typescript-sdk.md](09-sdks/03-typescript-sdk.md)~~ | **POSTPONED — v1.1+.** Browser + Node.js, npm packaging, typed generics, tree-shakeable exports |
| 9.4 | ~~[04-go-sdk.md](09-sdks/04-go-sdk.md)~~ | **POSTPONED — v1.1+.** Idiomatic Go, context propagation, pkg.go.dev, interface-based design |

### Deferred SDKs (Post-v1.0)

The following SDKs are **deferred to v1.1+** to focus v1.0 on the Python SDK (`openzep-py`):

| # | SDK | Planned Version | Key Features |
|---|-----|----------------|--------------|
| 9.3 | TypeScript SDK (`OpenZep-ts`) | v1.1+ | Browser + Node.js, npm packaging, typed generics, tree-shakeable exports |
| 9.4 | Go SDK (`OpenZep-go`) | v1.1+ | Idiomatic Go, context propagation, pkg.go.dev, interface-based design |

---

## Wave 5 — MCP, Dashboard, Observability, Deployment & Testing

Production readiness and developer experience.

| # | Document | Key Contents |
|---|----------|-------------|
| 10.1 | [01-mcp-setup.md](10-mcp-server/01-mcp-setup.md) | Stdio + SSE transport, JSON-RPC handlers, auth integration |
| 10.2 | [02-tool-definitions.md](10-mcp-server/02-tool-definitions.md) | All 8+ MCP tools with schemas, error responses, usage examples |
| 10.3 | [03-claude-desktop-config.md](10-mcp-server/03-claude-desktop-config.md) | mcpServers JSON config, testing instructions, debugging tips |
| 11.1 | [01-nextjs-setup.md](11-dashboard/01-nextjs-setup.md) | App Router, auth flow, auto-generated API client from OpenAPI |
| 11.2 | [02-tenant-management.md](11-dashboard/02-tenant-management.md) | Org CRUD, API key management UI, quota configuration |
| 11.3 | [03-user-graph-explorer.md](11-dashboard/03-user-graph-explorer.md) | Cytoscape.js integration, entity visualisation, search |
| 11.4 | [04-analytics-panels.md](11-dashboard/04-analytics-panels.md) | Grafana Mimir queries, usage charts, API call stats |
| 12.1 | [01-structured-logging.md](12-observability/01-structured-logging.md) | structlog config, field conventions (trace_id, org_id, user_id), PII redaction |
| 12.2 | [02-metrics-definitions.md](12-observability/02-metrics-definitions.md) | All Prometheus metrics, labels, alert thresholds, aggregation |
| 12.3 | [03-opentelemetry-tracing.md](12-observability/03-opentelemetry-tracing.md) | Trace propagation: HTTP → ARQ → LLM → Graphiti, span attributes |
| 12.4 | [04-grafana-dashboards.md](12-observability/04-grafana-dashboards.md) | Dashboard JSON definitions, RED method, cache hit rate, per-tenant |
| 13.1 | [01-docker-compose-dev.md](13-deployment/01-docker-compose-dev.md) | Dev Compose with health checks, volumes, networks, alloy |
| 13.2 | [02-docker-compose-prod.md](13-deployment/02-docker-compose-prod.md) | Production Compose with pgBouncer, Redis Sentinel, resource limits |
| 13.3 | [03-helm-chart.md](13-deployment/03-helm-chart.md) | K8s with HPA, PDB, PVs, NetworkPolicies, External Secrets |
| 13.4 | [04-environment-variables.md](13-deployment/04-environment-variables.md) | Complete env var reference: all vars, defaults, descriptions, example values |
| 13.5 | [05-migration-runbook.md](13-deployment/05-migration-runbook.md) | Alembic workflow, zero-downtime patterns, rollback procedures |
| 13.6 | [06-air-gapped-deployment.md](13-deployment/06-air-gapped-deployment.md) | Offline Docker images, model distribution, private PyPI/npm mirrors |
| 13.7 | [07-backup-disaster-recovery.md](13-deployment/07-backup-disaster-recovery.md) | Backup schedules, restore procedures, RTO/RPO targets |
| 14.1 | [01-test-strategy.md](14-testing/01-test-strategy.md) | Unit vs integration vs e2e vs perf vs security — what, when, how |
| 14.2 | [02-test-infrastructure.md](14-testing/02-test-infrastructure.md) | Testcontainers, fixtures, mock LLM suite, factory patterns |
| 14.3 | [03-golden-datasets.md](14-testing/03-golden-datasets.md) | Entity extraction, fact extraction, classification, retrieval — annotated datasets |
| 14.4 | [04-cross-tenant-test-matrix.md](14-testing/04-cross-tenant-test-matrix.md) | N tenants × M resources × K access methods — 403 vs 404, automated |
| 14.5 | [05-load-test-spec.md](14-testing/05-load-test-spec.md) | Hardware, dataset, request mix, ramp profile, pass/fail criteria |
| 14.6 | [06-ci-pipeline.md](14-testing/06-ci-pipeline.md) | GitLab CI stages, parallelization, scheduled runs, artifact retention |

---

## How to Use These Documents

1. **Before writing code in a domain:** Read the corresponding implementation doc first. It contains all design decisions, edge cases, and code patterns you need.

2. **During code review:** Reference the implementation doc. Code should match the documented patterns. Divergence requires an update to both the code and the doc.

3. **Phasing alignment:** Each document is tagged with the build phase it maps to (Phase 0-5 from the SRS). Focus on Phase 0 docs first.

4. **Living documents:** Update these docs when you discover edge cases or make design changes during implementation. The doc is the source of truth — code follows.

---

## Quick Reference by SRS Build Phase

| Phase | Docs to Read First |
|-------|-------------------|
| **Phase 0** Foundation | 1.1, 1.2, 1.3, 2.1, 2.2, 2.3, 2.4, 8.1, 8.2, 8.4, 12.1, 12.2, 12.3, 13.1, 13.4, 13.5, 14.2 |
| **Phase 1** Core Memory | 3.1, 3.2, 3.3, 3.4, 3.5, 4.1, 4.2, 4.3, 5.2, 5.3, 6.1, 6.2, 6.3, 6.4, 6.5, 7.1, 7.2 |
| **Phase 2** Python SDK Parity | 4.4, 4.5, 5.1, 5.7, 6.6, 7.3, 8.3, 8.5, 9.1, 9.2, 10.1, 10.2, 10.3 |
| **Phase 3** NLP Enrichment | 5.4, 5.5, 5.6, 14.3 |
| **Phase 4** Dashboard & SDKs | 11.1, 11.2, 11.3, 11.4, 12.4, 13.2, 13.3, 13.6 |
| **Phase 5** Hardening | 13.7, 14.1, 14.4, 14.5, 14.6 |

---

---

## Phase-by-Phase Development Timeline

The definitive project plan is at **[15-development-timeline/00-master-timeline.md](15-development-timeline/00-master-timeline.md)**.

This single document synthesises inputs from all six specialist reviews and provides:

- **Calibrated durations** (20 weeks, 8 phases — optimised for proprietary control)
- **Task-level breakdown** per phase with effort estimates (person-days)
- **Parallel tracks** (A: API/Core, B: NLP/Workers, C: DevOps/Infra)
- **Senior vs junior allocation** per task
- **42 measurable exit criteria** across all phases (G0.1 through G5b.8)
- **Risk gates** that must pass before progressing between phases
- **Teaching sessions** scheduled before each phase
- **Technical debt log** with incurrence and payback phases
- **Resource ramp** showing team composition over time

---

## Proprietary Boundaries

See **[16-proprietary-boundaries.md](16-proprietary-boundaries.md)** for the complete strategy on proprietary code protection, open-source boundaries, and commercial licensing.

---

## LLM BYOK Strategy

See **[17-llm-byok-strategy.md](17-llm-byok-strategy.md)** for the bring-your-own-key LLM strategy covering supported providers, key rotation, and fallback behaviour.

---

*Generated from `SRS_MemGraph.md` v1.0.0. All requirement IDs (AUTH-01, ING-01, etc.) are preserved in each domain doc for traceability.*
