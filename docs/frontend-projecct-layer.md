Now I have all the data. Let me compile the comprehensive frontend plan.

---

## Frontend Project-Scoping Plan

### Architecture Summary

| Decision | Chosen |
|---|---|
| URL structure | `/projects/[id]/*` — GitHub-repo style |
| Sidebar | Contextual — project-specific items when inside a project |
| Entry without project | Redirect to `/projects` |
| Current project tracking | React Context (`ProjectProvider`) reading from URL |
| Old route handling | Remove old flat routes entirely |
| Implementation | Full migration in one pass |

### Files to Create (8 new files)

| # | File | Purpose |
|---|---|---|
| 1 | `stores/project-context.tsx` | `ProjectProvider` + `useProject()` hook. Reads `projectId` from URL path, fetches project metadata (id, name, role), provides to children. Caches in state, refetches on projectId change. |
| 2 | `app/(dashboard)/projects/page.tsx` | **Project list**. Table/cards showing user's projects. Create project dialog (name, description). Each row → link to `/projects/[id]/sessions`. Empty state → "Create your first project". |
| 3 | `app/(dashboard)/projects/[id]/layout.tsx` | **Project layout**. Wraps `ProjectProvider`. Renders the contextual sidebar section for the project. Provides projectId to all children via context. |
| 4 | `app/(dashboard)/projects/[id]/page.tsx` | **Project overview**. Redirect to `/projects/[id]/sessions`. Or show mini dashboard with stats (session count, fact count, member count) per project. |
| 5 | `app/(dashboard)/projects/[id]/settings/page.tsx` | **Project settings**. Edit name, description. Archive project (with confirm dialog). Delete? (Backend: archive only). |
| 6 | `app/(dashboard)/projects/[id]/members/page.tsx` | **Member management**. Table of members (name, email, role). Owner can add members (search user → add as member), remove members (not last owner), change roles. |
| 7 | `app/(dashboard)/projects/[id]/sessions/page.tsx` | **Session list** (moved from `/sessions`). Remove user dropdown. All sessions for this project. Create/delete dialogs. |
| 8 | `app/(dashboard)/projects/[id]/memory/page.tsx` | **Memory page** (moved from `/memory`). Remove user dropdown. Ingest/Context/Search tabs now project-scoped. |

### Files to Move (7 files — same content, new location)

These are purely moves with minimal code changes — see "Modifications" below.

| File | New Location |
|---|---|
| `sessions/[id]/page.tsx` | `projects/[id]/sessions/[sessionId]/page.tsx` |
| `sessions/[id]/tabs.tsx` | `projects/[id]/sessions/[sessionId]/tabs.tsx` |
| `sessions/[id]/messages/page.tsx` | `projects/[id]/sessions/[sessionId]/messages/page.tsx` |
| `sessions/[id]/facts/page.tsx` | `projects/[id]/sessions/[sessionId]/facts/page.tsx` |
| `sessions/[id]/graph/page.tsx` | `projects/[id]/sessions/[sessionId]/graph/page.tsx` |
| `sessions/[id]/classifications/page.tsx` | `projects/[id]/sessions/[sessionId]/classifications/page.tsx` |
| `sessions/[id]/extractions/page.tsx` | `projects/[id]/sessions/[sessionId]/extractions/page.tsx` |

### Files to Modify (5 existing files)

| # | File | Changes |
|---|---|---|
| 1 | `app/(dashboard)/layout.tsx` | **Sidebar restructure**. When path matches `/projects/[id]/*`, show project section (Sessions, Memory, Graph, Members, Settings) under a project header. When not in a project, show "Projects" link in Data section. Use `useProject()` to get project name for header. |
| 2 | `app/(dashboard)/graph/page.tsx` | **Moved** to `/projects/[id]/graph/page.tsx`. Remove first-user fetch, use `useProject()` for `projectId`. All API calls change from `/v1/users/{userId}` to `/v1/projects/{projectId}`. |
| 3 | `app/(dashboard)/graph/communities/page.tsx` | **Moved** to `/projects/[id]/graph/communities/page.tsx`. Same pattern — no more first-user fetch, use `useProject()`. |
| 4 | `components/force-graph.tsx` | `ApiConfig` type: replace `userId: string` with `projectId: string`. Update node detail URL from `/v1/users/${userId}/graph/nodes/${id}` to `/v1/projects/${projectId}/graph/nodes/${id}`. |
| 5 | `lib/api-client.ts` | **No change needed** — the client is path-agnostic. All changes are at call sites. |

### Files to Delete (remove old routes)

| File | Reason |
|---|---|
| `app/(dashboard)/sessions/page.tsx` | Moved to `projects/[id]/sessions/page.tsx` |
| `app/(dashboard)/sessions/[id]/page.tsx` | Moved |
| `app/(dashboard)/sessions/[id]/tabs.tsx` | Moved |
| `app/(dashboard)/sessions/[id]/messages/page.tsx` | Moved |
| `app/(dashboard)/sessions/[id]/facts/page.tsx` | Moved |
| `app/(dashboard)/sessions/[id]/graph/page.tsx` | Moved |
| `app/(dashboard)/sessions/[id]/classifications/page.tsx` | Moved |
| `app/(dashboard)/sessions/[id]/extractions/page.tsx` | Moved |
| `app/(dashboard)/graph/page.tsx` | Moved |
| `app/(dashboard)/graph/communities/page.tsx` | Moved |
| `app/(dashboard)/memory/page.tsx` | Moved to `projects/[id]/memory/page.tsx` |

### API Call Migrations (27 sites → 0)

Every `/v1/users/{userId}/...` call for project-scoped data becomes `/v1/projects/{projectId}/...`. Sites:

| Endpoint | Replaced By | Files Affected |
|---|---|---|
| `POST /v1/users/{id}/memory` | `POST /v1/projects/{id}/memory` | `memory/page.tsx` ingest |
| `GET /v1/users/{id}/context?query=` | `GET /v1/projects/{id}/context?query=` | `memory/page.tsx` context |
| `GET /v1/users/{id}/search?query=` | `GET /v1/projects/{id}/search?query=` | `memory/page.tsx` search |
| `GET /v1/users/{uid}/sessions` | `GET /v1/projects/{pid}/sessions` | `sessions/page.tsx` |
| `POST /v1/users/{uid}/sessions` | `POST /v1/projects/{pid}/sessions` | `sessions/page.tsx` |
| `DELETE /v1/users/{uid}/sessions/{sid}` | `DELETE /v1/projects/{pid}/sessions/{sid}` | `sessions/page.tsx` |
| `GET /v1/users/{uid}/sessions/{sid}` | `GET /v1/projects/{pid}/sessions/{sid}` | `sessions/[id]/page.tsx` |
| `GET /v1/users/{uid}/sessions/{sid}/messages` | `GET /v1/projects/{pid}/sessions/{sid}/messages` | messages, facts, classifications, extractions |
| `GET /v1/users/{uid}/graph/nodes` | `GET /v1/projects/{pid}/graph/nodes` | graph pages ×2, force-graph |
| `GET /v1/users/{uid}/graph/edges` | `GET /v1/projects/{pid}/graph/edges` | graph pages ×2 |
| `GET /v1/users/{uid}/graph/communities` | `GET /v1/projects/{pid}/graph/communities` | communities page |

### Key Patterns in the Migration

**Before (current pattern on sessions page):**
```typescript
// Sessions page fetches users, shows dropdown, passes userId
const [selectedUserId, setSelectedUserId] = useState("");
useEffect(() => {
  const data = await get("/v1/users?limit=200"); // fetch ALL users
  setSelectedUserId(data.data[0].id); // auto-select first
}, []);
// API call:
await get(`/v1/users/${selectedUserId}/sessions?limit=50`);
```

**After (project-scoped pattern):**
```typescript
const { projectId } = useProject(); // from React Context
// No user fetch, no user dropdown
// API call:
await get(`/v1/projects/${projectId}/sessions?limit=50`);
```

**Before (session detail — passes userId in query params):**
```typescript
const userId = searchParams.get("userId") ?? "";
router.push(`/sessions/${sessionId}?userId=${session.user_id}`);
```

**After (session detail — projectId from context):**
```typescript
const { projectId } = useProject();
// Link is now context-free:
router.push(`/projects/${projectId}/sessions/${sessionId}`);
// `SessionTabs` no longer receives or passes userId
```

### ProjectProvider Design

```typescript
// stores/project-context.tsx
interface ProjectInfo {
  id: string;
  name: string;
  description: string | null;
  role: "owner" | "member";
  member_count: number;
}

interface ProjectContextValue {
  project: ProjectInfo | null;
  loading: boolean;
  error: string | null;
  refetch: () => void;
}

// Provider reads projectId from useParams(), fetches GET /v1/projects/{id}
// Wraps around project-scoped routes in projects/[id]/layout.tsx
// useProject() hook returns current project info — used by all sub-pages + sidebar
```

### Sidebar Restructure

**When NOT in a project (`/overview`, `/users`, `/settings/*`, `/audit`):**
```
Insights
  ├ Overview
  ├ Analytics
  └ Monitoring

Projects              ← NEW — links to /projects
  └ [Projects]

Administration
  ├ Users
  ├ API Keys
  ├ Extraction Schemas
  └ ... (rest of config)

System
  ├ Audit Log
  └ Settings
```

**When IN a project (`/projects/[id]/...`):**
```
Back to Projects      ← NEW — links to /projects

{Project Name}        ← Shows actual project name from context
  ├ Sessions          ← Links to /projects/[id]/sessions
  ├ Memory            ← Links to /projects/[id]/memory
  ├ Graph Explorer    ← Links to /projects/[id]/graph
  └ Communities       ← Links to /projects/[id]/graph/communities

Project Settings
  ├ Members           ← Links to /projects/[id]/members
  └ Settings          ← Links to /projects/[id]/settings

─  separador  ─

Administration        ← Same as above (org-level)
System                ← Same as above
```

### SessionTabs Replacement

The current `SessionTabs` component passes `?userId=` in every tab link:
```typescript
href={`/sessions/${sessionId}/${tab.href}?userId=${userId}`}
```

**After migration**, the tabs are inside `/projects/[id]/sessions/[sessionId]/...` so the project ID is in the URL. The tabs can either:
- Use relative links: `./messages`, `./facts`, etc.
- Or read `projectId` from context: `/projects/${projectId}/sessions/${sessionId}/messages`

**Recommendation**: Use `useProject()` to get `projectId`, construct full paths for clarity:
```typescript
const { projectId } = useProject();
href={`/projects/${projectId}/sessions/${sessionId}/messages`}
```

### Implementation Order

| Step | Description | Files | Est. |
|---|---|---|---|
| 1 | Create `ProjectContext` + `useProject()` hook | `stores/project-context.tsx` | 1h |
| 2 | Create project layout with `ProjectProvider` | `projects/[id]/layout.tsx` | 0.5h |
| 3 | Create project list page | `projects/page.tsx` | 2h |
| 4 | Create project settings page | `projects/[id]/settings/page.tsx` | 1h |
| 5 | Create project members page | `projects/[id]/members/page.tsx` | 2h |
| 6 | Restructure sidebar in dashboard layout | `layout.tsx` | 2h |
| 7 | Move + adapt sessions list | `projects/[id]/sessions/page.tsx` | 1.5h |
| 8 | Move + adapt session detail + all tabs | 7 files under `projects/[id]/sessions/[sessionId]/` | 2h |
| 9 | Move + adapt memory page | `projects/[id]/memory/page.tsx` | 1.5h |
| 10 | Move + adapt graph explorer | `projects/[id]/graph/page.tsx` | 1h |
| 11 | Move + adapt communities | `projects/[id]/graph/communities/page.tsx` | 0.5h |
| 12 | Update `ForceGraph` component | `components/force-graph.tsx` | 0.5h |
| 13 | Delete old route files | 10 files | 0.25h |
| 14 | End-to-end testing | — | 2h |

**Total estimate: 16-18 hours (~2-3 days)**

### Risks & Mitigations

| Risk | Mitigation |
|---|---|
| **Breakage from removing old routes** — any bookmarks or external links to `/sessions/*` will 404 | Add a PR note about the breaking change. The backend also removes old endpoints, so this is coordinated. |
| **`ForceGraph` has 922 lines** — modifying `ApiConfig` affects node-detail fetching which could be fragile | Change is minimal: `userId → projectId` in one interface + one URL. The D3 logic is untouched. |
| **No tests exist** — can't catch regressions | Manual testing checklist: create project, add member, view sessions, view memory, view graph, switch projects, verify member permissions. |
| **Hardcoded `http://localhost:8000`** in `layout.tsx` line 224 | Clean up to use `API_BASE` from api-client. Already inconsistent — fix as part of the `layout.tsx` modifications. |

### Open Design Question

The `layout.tsx` line 224 fetch is for displaying the current user's email in the topbar. This uses `fetch(http://localhost:8000/v1/users/${userId})` — should we:
1. Use the `api-client` `get()` method for consistency?
2. Keep it raw since it's fetching user profile data (not project data)?
3. Move the user profile fetch into the `RequireAuth` component so it's available app-wide?

I recommend (1) — use the api-client for consistency, no need to over-complicate. It's a minor change alongside the sidebar restructure.

---

Does this plan look right to you? Any adjustments before I start implementing?
