Good. I now have all the design decisions. Let me compile the full plan.

---

## Final Plan: Project-Scoped Session Restructuring

### Design Decisions Summary

| Decision | Choice |
|---|---|
| Project semantics | Collaborative workspace with multiple users |
| Access control | Owner + Member (two-level) |
| API URL structure | Replace user-scoped with project-scoped |
| User in project | Sessions belong to project; all members see all |
| All endpoints | Move to project scope (context, search, facts, graph) |
| Memory subject | Authenticated user (from JWT/API key) |
| Existing data | Auto-create personal project per user during migration |
| Graph scoping | Per-project graph (isolated) |
| API key scoping | Optional project-scoped API keys |
| Default session | Auto-create `__default__` per project |
| Member removal | Sessions stay in project |

---

### Phase 1: New Models & Migrations

**1.1 — `projects` table** (new model `models/project.py`)
```
id              UUID (PK, gen_random_uuid)
organization_id UUID (FK → organizations, CASCADE, NOT NULL, indexed)
name            TEXT (NOT NULL)
description     TEXT (nullable)
metadata        JSONB (NOT NULL, default '{}')
is_archived     BOOLEAN (default false, nullable)
created_by      UUID (FK → users, SET NULL, nullable) — who created it
created_at      TIMESTAMPTZ
updated_at      TIMESTAMPTZ
```
- Unique: `(organization_id, name)` — project names unique within org.
- RLS: `organization_id = current_setting('app.org_id')`.

**1.2 — `project_members` table** (new model `models/project_member.py`)
```
id              UUID (PK)
project_id      UUID (FK → projects, CASCADE, NOT NULL, indexed)
user_id         UUID (FK → users, CASCADE, NOT NULL)
role            VARCHAR(20) (NOT NULL, CHECK: owner/member)
created_at      TIMESTAMPTZ
updated_at      TIMESTAMPTZ
```
- Unique: `(project_id, user_id)` — a user can only be added once to a project.
- Index: `(user_id)` — quick lookup of user's projects.

**1.3 — Add `project_id` to existing tables**

| Table | New Column | Constraint | Notes |
|---|---|---|---|
| `sessions` | `project_id` | FK → projects, NOT NULL, indexed | Unique constraint changes from `(user_id, external_id)` → `(project_id, external_id)`. Drop old constraint. |
| `episodes` | `project_id` | FK → projects, NOT NULL, indexed | Denormalized for query efficiency. Can derive via session, but all queries filter by project_id so this avoids a join. |
| `facts` | `project_id` | FK → projects, NOT NULL, indexed | Same reasoning — all fact queries will be scoped by project. |
| `graph_entities` | `project_id` | FK → projects, NOT NULL, indexed | Per-project graph isolation. |
| `graph_relationships` | `project_id` | FK → projects, NOT NULL, indexed | (Created via raw SQL in migration 0006 — add column there.) |
| `graph_episode_entities` | `project_id` | FK → projects, NOT NULL, indexed | Junction table. |
| `structured_extractions` | `project_id` | FK → projects, SET NULL, nullable | Can derive via episode, but for RLS efficiency. |
| `dialog_classifications` | `project_id` | FK → projects, SET NULL, nullable | Same pattern. |
| `api_keys` | `project_id` | FK → projects, SET NULL, nullable | Null = org-wide access (backward compatible). Not-null = scoped to that project. |

**1.4 — Migration for existing data**
- Alembic migration that:
  1. Creates `projects` and `project_members` tables.
  2. For each existing user, creates a project `"{user.name}'s Project"` (or `"Personal"`) and adds them as `owner`.
  3. Adds `project_id` columns (initially nullable).
  4. Backfills `project_id` on `sessions` by finding each session's user → their personal project.
  5. Backfills `project_id` on `episodes` by joining through `session.project_id`.
  6. Backfills `project_id` on `facts` by joining through `source_episode_id → session.project_id`.
  7. Backfills `project_id` on graph tables similarly.
  8. Sets `project_id` to `NOT NULL` on all tables.
  9. Drops old unique constraint on sessions `(user_id, external_id)`, adds new one `(project_id, external_id)`.
  10. Adds indexes on all new `project_id` columns.

---

### Phase 2: Schema Layer

**2.1 — New Pydantic schemas** (new file `schemas/projects.py`)
- `CreateProjectRequest`: `name` (1-255), `description` (optional), `metadata` (optional)
- `UpdateProjectRequest`: `name`, `description`, `metadata`, `is_archived`
- `ProjectResponse`: `id`, `name`, `description`, `metadata`, `is_archived`, `member_count`, `created_by`, `created_at`, `updated_at`
- `ProjectListResponse`: lightweight version for list views
- `AddMemberRequest`: `user_id`, `role`
- `ProjectMemberResponse`: `id`, `user_id`, `role`, `created_at`

**2.2 — Modify existing schemas**
- `CreateSessionRequest`: add `project_id`? No — derived from URL path.
- `SessionResponse`: keep `user_id` as `created_by`, add `project_id`.
- `MessageResponse`: no change needed.
- `FactBatchRequest`: keep as-is, `session_id` is optional.
- `IngestMemoryRequest`: no change (session_id optional).

---

### Phase 3: Repository Layer

**3.1 — New `ProjectRepository`** (`repositories/project_repository.py`)
- `create(org_id, name, description, metadata, created_by)` → creates project + adds creator as owner (in a transaction)
- `get_by_id(org_id, project_id)` → single project
- `list_by_user(org_id, user_id)` → all projects where user is a member
- `list_all(org_id, limit, cursor)` → all projects in org (admin)
- `update(org_id, project_id, ...)` → update fields
- `archive(org_id, project_id)` → soft-delete
- `get_members(org_id, project_id)` → list members with roles
- `add_member(org_id, project_id, user_id, role)` → add user
- `remove_member(org_id, project_id, user_id)` → remove user (only if not last owner)
- `is_member(org_id, project_id, user_id)` → boolean check
- `get_role(org_id, project_id, user_id)` → role string or None

**3.2 — Modify `SessionRepository`**
- All queries: replace org-scoping via `.join(User, ...)` → `.join(Project, ...).where(Project.organization_id == org_id, Session.project_id == project_id)`
- `create()`: add `project_id` parameter
- `get_by_external_id()`: change unique lookup key from `(org_id, user_id, external_id)` to `(project_id, external_id)`
- `get_by_uuid()`: add `project_id` filter instead of `user_id`
- `list()`: change from user-scoped to project-scoped
- `get_or_create_default()`: change from per-user to per-project
- Delete: remove `user_id` parameter where it was used for intra-org isolation (now replaced by project membership check in service/router)

**3.3 — Modify `EpisodeRepository`**
- Add `project_id` to `batch_create()`
- All search/query methods: add `project_id` filter alongside `organization_id`
- `search_by_vector()` / `search_by_bm25()`: change from `user_id` param to `project_id`

**3.4 — Modify `FactRepository`**
- Add `project_id` to `create()`, `batch_create()`, `batch_create_or_skip()`
- `list_by_session()`: already session-scoped, derive project through session
- `search_by_vector()` / `search_by_bm25()`: change from `user_id` to `project_id`
- `get_entities_for_session()`: add project scoping
- `soft_delete_by_user()`: change to `soft_delete_by_project()` or keep user-scoped for GDPR

**3.5 — Modify `EntityRepository`** (graph)
- All operations: add `project_id` to queries
- The graph backend (PostgresGraphBackend) reads/writes `graph_entities` — pass `project_id` to all calls

**3.6 — Modify `StructuredExtractionRepository`**
- Add `project_id` to queries
- `list_by_session()`: derive project through session

**3.7 — Modify `DialogClassificationRepository`**
- Add `project_id` to queries

**3.8 — Modify `ApiKeyRepository`**
- Add optional `project_id` filter to auth queries
- When a key has `project_id` set, only allow access to that project

---

### Phase 4: Service Layer

**4.1 — New `ProjectService`** (`services/project_service.py`)
- `create_project(org_id, name, description, metadata, created_by)` — creates project + adds creator as owner
- `list_projects(org_id, user_id)` — list projects for user
- `get_project(org_id, project_id)` — get project details
- `update_project(org_id, project_id, ...)` — update project
- `archive_project(org_id, project_id)` — archive
- `add_member(org_id, project_id, user_id, role)` — validate user exists, add member
- `remove_member(org_id, project_id, user_id)` — validate not last owner
- `is_member(org_id, project_id, user_id)` — membership check
- `require_membership(org_id, project_id, user_id)` — raise 403 if not member

**4.2 — Modify `SessionService`**
- Remove `user_id` from method signatures where it was used for scoping
- Add `project_id` parameter to all methods
- `create_session(organization_id, project_id, user_id, external_id, metadata)` — `user_id` is the authenticated creator
- `list_sessions(org_id, project_id, limit, cursor, ...)` — scoped by project, not user
- `get_session(org_id, project_id, session_id)` — scoped by project
- `delete_session(org_id, project_id, session_id, user_id)` — scoped by project

**4.3 — Modify `MemoryService`**
- `ingest(org_id, project_id, messages, session_external_id, idempotency_key)` — no user_id URL param
- Resolve user from auth context (passed from router, from JWT claims)
- `_resolve_session()`: scoped by `project_id` instead of `user_id`
- `_resolve_user()`: unchanged (still org-scoped)
- Content dedup hash: include `project_id` instead of `user_id`
- ARQ task enqueue: include `project_id` in all task params

**4.4 — Modify `ContextService`**
- `assemble(project_id, query, limit, format)` — search across project's memory
- Remove `user_id` param

**4.5 — Modify `FactService`**
- `ingest_facts(org_id, project_id, user_uuid, facts, session_external_id)` — scoped by project
- `list_facts_by_session()` — already session-scoped

**4.6 — Modify `GraphService`**
- All methods: add `project_id` parameter
- `get_entities(org_id, project_id, ...)` — scoped by project
- `get_entity(org_id, project_id, entity_id)`
- `delete_entity(org_id, project_id, entity_id)`
- `get_communities(org_id, project_id)`

**4.7 — Modify `ClassificationService`**
- Add `project_id` to all queries
- Verify session belongs to project

**4.8 — Modify `StructuredExtractionService`**
- Add `project_id` to all queries
- Verify session belongs to project

---

### Phase 5: Router Layer (New Project-Scoped Endpoints)

**5.1 — New `routers/projects.py`** (project CRUD + members)
| Method | Path | Handler | Description |
|---|---|---|---|
| POST | `/v1/projects` | `create_project` | Create project (auth user = owner) |
| GET | `/v1/projects` | `list_projects` | List projects where auth user is member |
| GET | `/v1/projects/{project_id}` | `get_project` | Get project details |
| PUT | `/v1/projects/{project_id}` | `update_project` | Update project (owner only) |
| DELETE | `/v1/projects/{project_id}` | `archive_project` | Archive project (owner only) |
| POST | `/v1/projects/{project_id}/members` | `add_member` | Add member (owner only) |
| DELETE | `/v1/projects/{project_id}/members/{user_id}` | `remove_member` | Remove member (owner only) |
| GET | `/v1/projects/{project_id}/members` | `list_members` | List project members |

**5.2 — Replace `routers/sessions.py`** (prefix: `/v1/projects/{project_id}/sessions`)
| Method | Path | Handler |
|---|---|---|
| POST | `/v1/projects/{project_id}/sessions` | `create_session` |
| GET | `/v1/projects/{project_id}/sessions` | `list_sessions` |
| GET | `/v1/projects/{project_id}/sessions/{session_id}` | `get_session` |
| GET | `/v1/projects/{project_id}/sessions/{session_id}/messages` | `get_session_messages` |
| GET | `/v1/projects/{project_id}/sessions/{session_id}/facts` | `get_session_facts` |
| DELETE | `/v1/projects/{project_id}/sessions/{session_id}` | `delete_session` |

**5.3 — Replace `routers/memory.py`** (prefix: `/v1/projects/{project_id}/memory`)
| Method | Path | Handler |
|---|---|---|
| POST | `/v1/projects/{project_id}/memory` | `ingest_messages` (auth user from JWT) |
| DELETE | `/v1/projects/{project_id}/memory` | `delete_project_memory` (soft-delete all project episodes + facts) |

**5.4 — Replace `routers/context.py`** (prefix: `/v1/projects/{project_id}/context`)
| Method | Path | Handler |
|---|---|---|
| GET | `/v1/projects/{project_id}/context` | `get_context` |

**5.5 — Replace `routers/search.py`** (prefix: `/v1/projects/{project_id}/search`)
| Method | Path | Handler |
|---|---|---|
| GET | `/v1/projects/{project_id}/search` | `search_memory` |

**5.6 — Replace `routers/facts.py`** (prefix: `/v1/projects/{project_id}/facts`)
| Method | Path | Handler |
|---|---|---|
| POST | `/v1/projects/{project_id}/facts` | `ingest_facts` |

**5.7 — Replace `routers/graph.py`** (prefix: `/v1/projects/{project_id}/graph`)
| Method | Path | Handler |
|---|---|---|
| GET | `/v1/projects/{project_id}/graph/nodes` | `list_graph_nodes` |
| GET | `/v1/projects/{project_id}/graph/nodes/{node_id}` | `get_graph_node` |
| DELETE | `/v1/projects/{project_id}/graph/nodes/{node_id}` | `delete_graph_node` |
| GET | `/v1/projects/{project_id}/graph/edges` | `list_graph_edges` |
| GET | `/v1/projects/{project_id}/graph/communities` | `list_communities` |

**5.8 — Replace `routers/classifications.py`** (prefix: `/v1/projects/{project_id}/sessions/{session_id}/classifications`)
| Method | Path | Handler |
|---|---|---|
| GET | `/v1/projects/{project_id}/sessions/{session_id}/classifications` | `list_classifications` |
| GET | `/v1/projects/{project_id}/sessions/{session_id}/classifications/{episode_id}` | `get_episode_classification` |

**5.9 — Replace `routers/structured_extractions.py`** (prefix: `/v1/projects/{project_id}/sessions/{session_id}/structured-extractions`)
| Method | Path | Handler |
|---|---|---|
| GET | `/v1/projects/{project_id}/sessions/{session_id}/structured-extractions` | `list_structured_extractions` |
| GET | `/v1/projects/{project_id}/sessions/{session_id}/structured-extractions/{episode_id}` | `get_episode_extraction` |

---

### Phase 6: Authorization Dependency

**6.1 — New dependency** `dependencies/project_auth.py`
```python
async def require_project_membership(
    project_id: UUID,
    org_id: str = Depends(require_org_id),
    request: Request = None,
) -> UUID:
    """Verify the authenticated user is a member of the specified project.
    
    For JWT users: checks project_members table.
    For API key users: checks if the key's project_id matches (or is null = org-wide).
    """
```

- This is injected into every project-scoped route.
- Resolves the project, checks the authenticated user's membership.
- For API keys with `project_id` set: only that project is allowed.
- For JWT dashboard users: membership is checked against `project_members`.

---

### Phase 7: Worker Changes

**7.1 — All ARQ workers** (extract_facts, extract_entities, embed_episode, classify_dialog, sync_to_graph, extract_structured)
- Add `project_id` to task parameters
- Workers pass `project_id` through to repository calls during enrichment

**7.2 — Graph sync worker** (`sync_to_graph`)
- Pass `project_id` when creating graph entities/relationships
- The graph backend uses `project_id` for isolation

**7.3 — Context cache invalidation**
- Change cache key pattern from `ctx:{org_id}:{user_id}:*` to `ctx:{org_id}:{project_id}:*`

---

### Phase 8: main.py Router Registration

Remove old routers, add new ones:

```python
# REMOVED:
# app.include_router(sessions.router)       # old /v1/users/{user_id}/sessions
# app.include_router(classifications.router)
# app.include_router(structured_extractions.router)
# app.include_router(memory.router)
# app.include_router(context.router)
# app.include_router(search.router)
# app.include_router(graph.router)
# app.include_router(facts.router)

# ADDED:
app.include_router(projects.router)                    # /v1/projects
app.include_router(sessions.router)                    # /v1/projects/{project_id}/sessions
app.include_router(classifications.router)             # /v1/projects/{project_id}/sessions/{session_id}/classifications
app.include_router(structured_extractions.router)      # /v1/projects/{project_id}/sessions/{session_id}/structured-extractions
app.include_router(memory.router)                      # /v1/projects/{project_id}/memory
app.include_router(context.router)                     # /v1/projects/{project_id}/context
app.include_router(search.router)                      # /v1/projects/{project_id}/search
app.include_router(graph.router)                       # /v1/projects/{project_id}/graph
app.include_router(facts.router)                       # /v1/projects/{project_id}/facts
```

---

### Phase 9: Implementation Order

| Step | What | Depends On | Effort Estimate |
|---|---|---|---|
| 1 | Alembic migration: create `projects` + `project_members` tables | Nothing | 1 day |
| 2 | Alembic migration: add `project_id` to all entity tables + backfill | Step 1 | 2 days |
| 3 | New models: `Project`, `ProjectMember` | Step 1 | 0.5 day |
| 4 | New schemas: `projects.py` | Step 3 | 0.5 day |
| 5 | `ProjectRepository` + `ProjectService` | Steps 3-4 | 1 day |
| 6 | `require_project_membership` dependency + auth changes | Step 5 | 1 day |
| 7 | Project management router (`/v1/projects` CRUD + members) | Steps 5-6 | 1 day |
| 8 | Modify `SessionRepository` + `SessionService` for project scoping | Step 2 | 1.5 days |
| 9 | Rewrite sessions router | Step 8 | 0.5 day |
| 10 | Modify `EpisodeRepository` for project scoping | Step 2 | 1 day |
| 11 | Modify `FactRepository` + `FactService` for project scoping | Step 2 | 1 day |
| 12 | Rewrite memory router + modify `MemoryService` | Steps 8, 10 | 1.5 days |
| 13 | Modify `ContextService` + `HybridRetriever` for project scope | Steps 10-11 | 1 day |
| 14 | Rewrite context/search routers | Step 13 | 0.5 day |
| 15 | Modify graph repositories + service for project scope | Step 2 | 1 day |
| 16 | Rewrite graph router | Step 15 | 0.5 day |
| 17 | Modify `StructuredExtractionService` + `ClassificationService` | Step 2 | 0.5 day |
| 18 | Rewrite classification + extraction routers | Step 17 | 0.5 day |
| 19 | Update ARQ workers to pass `project_id` | Steps 8-11 | 1 day |
| 20 | Update `main.py` router registration | All above | 0.5 day |
| 21 | Update tests (all layers) | All above | 2-3 days |
| 22 | Integration testing + rollout | All above | 1 day |

**Total estimate: 16-20 days**

---

### Key Risks & Mitigations

| Risk | Mitigation |
|---|---|
| **Data migration** — backfilling `project_id` on millions of rows could lock tables | Use batched UPDATE with `WHERE id IN (...)` in chunks of 1000, run during maintenance window. Test on staging first. |
| **Client SDK breakage** — all old user-scoped endpoints disappear | Coordinate release with SDK updates. Version-bump the API to v2. Add deprecation headers on old endpoints for a transition period. |
| **API key project scoping** — existing keys have `project_id=NULL` which grants org-wide access | Backward compatible. New keys can optionally set `project_id`. Document clearly. |
| **Graph backend changes** — PostgresGraphBackend needs `project_id` support | The graph backend reads/writes raw SQL — these need careful `project_id` filters added to every query. |
| **RLS complexity** — adding project-level isolation on top of org-level RLS | Keep RLS at org level only. Project scoping in application layer. No need for `app.project_id` session variable. |

---

Do you want me to go deeper into any specific phase, or shall I proceed with breaking this into executable tasks?
