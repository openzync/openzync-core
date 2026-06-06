Here's the breakdown of Phase 2 into 3 subphases.
Phase 2 — Full Feature Parity: Subphase Plan
Duration: Weeks 7–9 (3 weeks)
Theme: "Python SDK ships, MCP server connects, graph queries perform, communities form."
Subphase Overview
Week 7             Week 8             Week 9
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│  2a: Graph   │  │  2b: SDK     │  │  2c: MCP     │
│              │  │              │  │              │
│ Graph query  │  │ Python SDK   │  │ MCP server   │
│ endpoints    │  │ sync/async   │  │ 8 tools      │
│ Client abst. │  │ 5 domains    │  │ Claude Desk. │
│ Full-text    │  │ PyPI CI      │  │ Communities  │
│ Biz data ing │  │              │  │ DevOps wrap  │
└──────┬───────┘  └──────┬───────┘  └──────┬───────┘
       │                 │                 │
       ▼                 ▼                 ▼
   G2.3, G2.5        G2.1             G2.2, G2.4
   (graph, fact      (SDK             (MCP, comm.
    ingestion)        published)       summaries)
Subphase 2a — Graph & Search (Week 7)
Theme: "Graph queries perform. Business data flows in. Full-text search is online."
Team: Senior A (Track A) + Senior B (Track B)
Day	Senior A
D1	🔒 services/graph_service.py — get_nodes(org_id, user_id, type_filter), get_node(node_id), get_edges(org_id, user_id, predicate_filter), delete_node(node_id) — wraps Graphiti client
D2	🔒 routers/graph.py — GET /users/{user_id}/graph/nodes, GET /users/{user_id}/graph/nodes/{node_id}, GET /users/{user_id}/graph/edges, DELETE /users/{user_id}/graph/nodes/{node_id}, GET /users/{user_id}/graph/communities, cursor pagination on all list endpoints
D3	🔒 Graph client abstraction: GraphBackend ABC + FalkorDBBackend impl in packages/graphiti-client/. org_id enforcement on every method. Graceful degrade when graphiti-core not installed.
D4	🔒 Graph pagination: cursor-based on node/edge lists, composite cursor (created_at, id), limit+1 for has_more
D5	Integration tests: graph query endpoints work, cross-tenant isolation, pagination
Exit criteria:
- ✅ GET /users/{user_id}/graph/nodes returns paginated entity nodes
- ✅ POST /users/{user_id}/facts accepts 500 triples → 202 + facts in DB
- ✅ GET /search?query=...&types=facts returns BM25 results
- ✅ Cross-tenant: Org B cannot see Org A's graph nodes
Key files to create:
NEW:
  services/graph_service.py           ← Graph query service
  routers/graph.py                    ← Graph query endpoints
  routers/facts.py                    ← Business data ingestion
  schemas/facts.py                    ← Fact batch schemas
  workers/tasks/ingest_business_data.py  ← Fact batch worker

MODIFIED:
  packages/graphiti-client/interface.py  ← GraphBackend ABC (if not done)
  packages/graphiti-client/backends/falkordb.py  ← Update if needed
  repositories/episode_repository.py  ← Verify search methods
  repositories/fact_repository.py      ← Verify search methods
Subphase 2b — Python SDK (Week 8)
Theme: "pip install memgraph-py works end-to-end."
Team: Senior A (Track A — SDK lead) + DevOps (Track C — PyPI CI)
Day	Senior A
D1	📦 SDK scaffold: oss/sdk-python/memgraph/ package, pyproject.toml, MemGraph client class with constructor + auth
D2	📦 SDK: client.memory.ingest() + client.memory.get_context() — async methods, httpx.AsyncClient, typed responses
D3	📦 SDK: client.facts.add() + client.facts.list() + client.graph.nodes() + client.graph.search() — all 5 domains
D4	📦 SDK: retry with exponential backoff (429/5xx), typed error hierarchy (MemGraphError, NotFoundError, RateLimitError), PaginatedAsyncIterator
D5	📦 SDK: sync wrapper (asyncio.run()), integration tests against running API, publish to TestPyPI
Exit criteria:
- ✅ pip install memgraph-py from TestPyPI → client.memory.ingest() returns typed response (G2.1)
- ✅ Sync + async interfaces both work
- ✅ SDK integration tests pass against running API in CI
- ✅ Prometheus /metrics endpoint returns worker metrics
- ✅ Production Docker Compose boots with pgBouncer + Sentinel
Key files to create:
NEW:
  oss/sdk-python/pyproject.toml           ← SDK package config
  oss/sdk-python/memgraph/__init__.py      ← Client class
  oss/sdk-python/memgraph/client.py        ← HTTP client
  oss/sdk-python/memgraph/memory.py        ← memory domain
  oss/sdk-python/memgraph/facts.py         ← facts domain
  oss/sdk-python/memgraph/graph.py         ← graph domain
  oss/sdk-python/memgraph/users.py         ← users domain
  oss/sdk-python/memgraph/sessions.py      ← sessions domain
  oss/sdk-python/memgraph/exceptions.py    ← error types
  oss/sdk-python/memgraph/pagination.py    ← PaginatedAsyncIterator

MODIFIED:
  infra/docker-compose.prod.yml           ← pgBouncer, Sentinel
  .gitlab-ci.yml                           ← SDK publish stage
Subphase 2c — MCP + Communities + DevOps (Week 9)
Theme: "MCP server connects to Claude Desktop. Community summaries run nightly."
Team: Senior B (Track B — MCP + Communities) + DevOps (Track C — Helm)
Day	Senior B
D1	📦 MCP server: services/mcp/server.py — stdio transport, JSON-RPC 2.0 handler, tool registry, MemGraphMCPServer class
D2	📦 MCP tools: add_memory, get_context, search_memory, add_fact, list_facts, get_user_graph, create_user, list_sessions — each maps to SDK client call
D3	📦 MCP SSE transport: aiohttp server with SSE endpoint, auth via API key header, CORS
D4	🔒 Community summarisation: workers/tasks/summarise_community.py — Label Propagation via networkx, LLM summary generation (OpenRouter), upsert CommunityNode + MEMBER_OF edges. Nightly schedule via ARQ low queue.
D5	MCP Claude Desktop config: claude_desktop_config.json example, testing guide. docs/implementation/10-mcp-server/03-claude-desktop-config.md update. Integration tests for all 8 MCP tools.
Exit criteria:
- ✅ MCP server starts with stdio + SSE. All 8 tools respond correctly (G2.2)
- ✅ Claude Desktop connects and calls add_memory → get_context
- ✅ Community summary generated for 5-entity cluster, CommunityNode in DB (G2.4)
- ✅ Helm chart passes helm lint and helm install --dry-run
- ✅ Grafana dashboards auto-provisioned
Key files to create:
NEW:
  services/mcp/server.py                 ← MCP server (stdio + SSE)
  services/mcp/tools.py                  ← 8 tool definitions
  services/mcp/__init__.py
  workers/tasks/summarise_community.py   ← Community detection worker

MODIFIED:
  infra/helm/memgraph/Chart.yaml         ← Helm chart
  infra/helm/memgraph/values.yaml        ← Values
  infra/helm/memgraph/templates/         ← Templates
  docs/implementation/10-mcp-server/03-claude-desktop-config.md  ← Config guide
Team Allocation Summary
Subphase	Duration	Senior A (Track A)
2a — Graph	5 days (W7)	💼 Graph service + endpoints
2b — SDK	5 days (W8)	💼 Python SDK (all 5 domains)
2c — MCP	5 days (W9)	🔧 Assists
Risk Register
Risk	Likelihood
Graphiti-core not installed — community detection and graph queries fail	High (no graphiti)
SDK sync/async duality bugs — asyncio.run() called from within running event loop	Medium
MCP protocol changes — MCP spec not yet 1.0	Low
Helm chart install fails on different K8s versions	Medium
Community detection OOM on large graphs	Low
Exit Criteria (Phase 2 gates)
Gate	Criterion
G2.1	pip install memgraph-py → client.memory.add() returns typed response
G2.2	MCP server starts with stdio + SSE. All 8 tools respond correctly
G2.3	Graph query: GET /graph/nodes with 15k entity nodes ≤500ms p99
G2.4	Community summary generated for 5-entity cluster
G2.5	Business data: POST 500 fact triples in < 5s
 	Helm chart: helm install --dry-run --debug passes
 	Python SDK published to TestPyPI
Teaching Sessions
When	Session
Start of 2a	Graph client abstraction + FalkorDB deep-dive
Start of 2b	SDK design patterns: sync/async duality, retry, error model
Start of 2c	MCP protocol: JSON-RPC, transports, tool registration
Start of 2c	Community detection: Label Propagation algorithm + LLM summarisation
Quick Reference
Subphase	Week	Focus	Senior A
2a	W7	Graph + Facts	Graph service/endpoints
2b	W8	Python SDK	Full SDK + PyPI
2c	W9	MCP + Communities	—
▣  Plan · DeepSeek V4 Flash · 1m 15s
