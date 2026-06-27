Overview
========

What is OpenZep?
----------------

OpenZep is an open-source agent memory platform designed to give AI agents
**persistent, queryable, graph-based memory**. It sits between your LLM
application and your data, providing:

- **Hybrid retrieval** — semantic (vector) search combined with graph traversal
  for context-aware memory recall.
- **Knowledge graphs** — entity extraction, relationship detection, and
  community detection over conversation history.
- **Temporal queries** — time-aware memory retrieval that respects when events
  occurred.
- **Multi-tenant isolation** — organizations, projects, and users are fully
  isolated at the data layer.

Architecture
------------

The system is built as a **FastAPI** application with:

- **PostgreSQL + pgvector** for relational data and vector embeddings.
- **Redis** for caching, rate limiting, and ARQ task queue.
- **Graph backend** (Postgres-based, with pluggable interface) for entity-relation
  storage.
- **ARQ worker pool** for background tasks: entity extraction, fact extraction,
  dialog classification, embedding computation.

Layer structure follows strict separation of concerns:

.. list-table:: Layer responsibilities
   :header-rows: 1

   * - Layer
     - Responsibility
   * - ``routers/``
     - HTTP adapter only — validates input, delegates to services
   * - ``services/``
     - All business logic — orchestrates call chains
   * - ``repositories/``
     - Database access — SQLAlchemy queries only
   * - ``models/``
     - ORM definitions — no business logic
   * - ``schemas/``
     - Pydantic request/response models — no ORM imports

Key features
------------

- **Agent sessions** with configurable tool sets and model selection.
- **Message ingestion** with automatic entity/fact extraction.
- **Graph-based memory** with community detection and temporal queries.
- **Human-in-the-loop** approval checkpoints for side-effecting actions.
- **Credit billing** with usage tracking across organizations.
- **Prompt templates** versioned and stored as Jinja2 files.
- **SDKs** — Python (openzync) and TypeScript client libraries.

Tech stack
----------

- **Runtime**: Python 3.11+ / FastAPI / uvicorn
- **Database**: PostgreSQL 15+ with pgvector
- **Cache / Queue**: Redis with ARQ
- **LLM**: OpenAI / Anthropic via pluggable backends
- **Deployment**: Docker, GitLab CI
