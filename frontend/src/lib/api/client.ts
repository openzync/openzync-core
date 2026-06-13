import type { components, paths } from "./schema";

type Schema = components["schemas"];

// ---- Auth Types ----

export interface AuthState {
  accessToken: string | null;
  refreshToken: string | null;
  user: Schema["DashboardUserResponse"] | null;
  isLoading: boolean;
}

// ---- API Client ----

const BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type TokenProvider = () => string | null;

class ApiError extends Error {
  status: number;
  body: Record<string, unknown> | null;

  constructor(status: number, body: Record<string, unknown> | null) {
    super(`API error ${status}`);
    this.status = status;
    this.body = body;
  }

  get detail(): string | undefined {
    if (this.body && typeof this.body.detail === "string") return this.body.detail;
    if (Array.isArray(this.body?.detail)) return (this.body.detail as Array<{ msg: string }>).map((e) => e.msg).join("; ");
    return undefined;
  }
}

class ApiClient {
  private _tokenProvider: TokenProvider | null = null;
  private _onUnauthorized: (() => void) | null = null;

  setTokenProvider(provider: TokenProvider | null) {
    this._tokenProvider = provider;
  }

  setOnUnauthorized(handler: (() => void) | null) {
    this._onUnauthorized = handler;
  }

  private _headers(extra?: Record<string, string>): Record<string, string> {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...extra,
    };
    const token = this._tokenProvider?.();
    if (token) {
      headers["Authorization"] = `Bearer ${token}`;
    }
    return headers;
  }

  async request<T>(
    method: string,
    path: string,
    body?: unknown,
    params?: Record<string, string | number | boolean | undefined | null>,
  ): Promise<T> {
    let url = `${BASE_URL}${path}`;

    if (params) {
      const search = new URLSearchParams();
      for (const [key, val] of Object.entries(params)) {
        if (val !== undefined && val !== null && val !== "") {
          search.set(key, String(val));
        }
      }
      const qs = search.toString();
      if (qs) url += `?${qs}`;
    }

    const response = await fetch(url, {
      method,
      headers: this._headers(),
      body: body ? JSON.stringify(body) : undefined,
    });

    if (!response.ok) {
      if (response.status === 401) {
        this._onUnauthorized?.();
      }
      const errorBody = await response.json().catch(() => null);
      throw new ApiError(response.status, errorBody);
    }

    if (response.status === 204) return undefined as T;

    return response.json() as Promise<T>;
  }

  get<T>(path: string, params?: Record<string, string | number | boolean | undefined | null>) {
    return this.request<T>("GET", path, undefined, params);
  }

  post<T>(path: string, body?: unknown) {
    return this.request<T>("POST", path, body);
  }

  patch<T>(path: string, body?: unknown) {
    return this.request<T>("PATCH", path, body);
  }

  delete<T>(path: string) {
    return this.request<T>("DELETE", path);
  }
}

export const api = new ApiClient();
export { ApiError };

// ---- Typed API helpers ----

export async function signup(
  payload: Schema["SignupRequest"],
): Promise<Schema["TokenResponse"]> {
  return api.post("/v1/auth/signup", payload);
}

export async function login(
  payload: Schema["LoginRequest"],
): Promise<Schema["TokenResponse"]> {
  return api.post("/v1/auth/login", payload);
}

export async function refresh(
  payload: Schema["RefreshRequest"],
): Promise<Schema["TokenResponse"]> {
  return api.post("/v1/auth/refresh", payload);
}

export async function getProfile(): Promise<Schema["DashboardUserResponse"]> {
  return api.get("/v1/auth/me");
}

export async function updateProfile(
  payload: Schema["UpdateProfileRequest"],
): Promise<Schema["DashboardUserResponse"]> {
  return api.patch("/v1/auth/me", payload);
}

// ---- Admin: Org Stats ----

export async function getOrgStats(): Promise<Schema["OrgStatsResponse"]> {
  return api.get("/v1/admin/stats/org");
}

export async function getUsageStats(
  days?: number,
): Promise<Schema["UsageStatsResponse"][]> {
  return api.get("/v1/admin/stats/usage", { days });
}

// ---- Admin: API Keys ----

export async function listApiKeys(): Promise<Schema["ApiKeyListResponse"]> {
  return api.get("/v1/admin/api-keys");
}

export async function createApiKey(
  payload: Schema["CreateApiKeyRequest"],
): Promise<Schema["ApiKeyCreatedResponse"]> {
  return api.post("/v1/admin/api-keys", payload);
}

export async function revokeApiKey(keyId: string): Promise<void> {
  return api.delete(`/v1/admin/api-keys/${keyId}`);
}

// ---- Users ----

export async function listUsers(
  params?: {
    limit?: number;
    cursor?: string | null;
    search?: string | null;
    created_after?: string | null;
    created_before?: string | null;
  },
): Promise<Schema["UserListResponse"]> {
  return api.get("/v1/users", params as Record<string, string | number | boolean | undefined | null>);
}

export async function createUser(
  payload: Schema["CreateUserRequest"],
): Promise<Schema["UserResponse"]> {
  return api.post("/v1/users", payload);
}

export async function getUser(
  userId: string,
): Promise<Schema["UserResponse"]> {
  return api.get(`/v1/users/${userId}`);
}

export async function updateUser(
  userId: string,
  payload: Schema["UpdateUserRequest"],
): Promise<Schema["UserResponse"]> {
  return api.patch(`/v1/users/${userId}`, payload);
}

export async function deleteUser(userId: string): Promise<void> {
  return api.delete(`/v1/users/${userId}`);
}

// ---- Sessions ----

export async function listSessions(
  projectId: string,
  userId: string,
  params?: {
    limit?: number;
    cursor?: string | null;
    include_closed?: boolean;
  },
): Promise<Schema["PaginatedResponse_SessionListResponse_"]> {
  return api.get(`/v1/projects/${projectId}/${userId}/sessions`, params as Record<string, string | number | boolean | undefined | null>);
}

export async function createSession(
  projectId: string,
  userId: string,
  payload: Schema["CreateSessionRequest"],
): Promise<Schema["SessionResponse"]> {
  return api.post(`/v1/projects/${projectId}/${userId}/sessions`, payload);
}

export async function getSession(
  projectId: string,
  userId: string,
  sessionId: string,
): Promise<Schema["SessionResponse"]> {
  return api.get(`/v1/projects/${projectId}/${userId}/sessions/${sessionId}`);
}

export async function deleteSession(projectId: string, userId: string, sessionId: string): Promise<void> {
  return api.delete(`/v1/projects/${projectId}/${userId}/sessions/${sessionId}`);
}

export async function getSessionMessages(
  projectId: string,
  userId: string,
  sessionId: string,
  params?: {
    limit?: number;
    cursor?: string | null;
  },
): Promise<Schema["PaginatedResponse_MessageResponse_"]> {
  return api.get(`/v1/projects/${projectId}/${userId}/sessions/${sessionId}/messages`, params as Record<string, string | number | boolean | undefined | null>);
}

export async function getSessionFacts(
  projectId: string,
  userId: string,
  sessionId: string,
  params?: {
    limit?: number;
    cursor?: string | null;
  },
): Promise<Schema["PaginatedResponse_FactResponse_"]> {
  return api.get(`/v1/projects/${projectId}/${userId}/sessions/${sessionId}/facts`, params as Record<string, string | number | boolean | undefined | null>);
}

// ---- Graph ----

export async function listGraphNodes(
  projectId: string,
  userId: string,
  params?: {
    limit?: number;
    cursor?: string | null;
    entity_type?: string | null;
  },
): Promise<Schema["GraphNodesListResponse"]> {
  return api.get(`/v1/projects/${projectId}/${userId}/graph/nodes`, params as Record<string, string | number | boolean | undefined | null>);
}

export async function getGraphNode(
  projectId: string,
  userId: string,
  nodeId: string,
): Promise<Schema["GraphNodeDetailResponse"]> {
  return api.get(`/v1/projects/${projectId}/${userId}/graph/nodes/${nodeId}`);
}

// ---- Admin: Metrics ----

export interface LatencyPercentiles {
  p50: number;
  p95: number;
  p99: number;
}

export interface EpisodeStats {
  added_total: number;
  added_24h: number;
  in_progress: number;
  enrichment_pending: number;
}

export interface GraphStats {
  entities_total: number;
  entities_24h: number;
  relationships_total: number;
}

export interface MetricsSummaryResponse {
  episodes: EpisodeStats;
  graphs: GraphStats;
  users_total: number;
  request_rate: Record<string, number>;
  error_rate_pct: number;
  overall_latency_ms: LatencyPercentiles;
  context_latency_ms: LatencyPercentiles;
  graph_search_latency_ms: LatencyPercentiles;
  queue_depth: { high: number; low: number } | null;
  total_requests: number;
  active_requests: number;
  status: string;
  message: string | null;
}

export interface PrometheusTarget {
  job: string;
  instance: string;
  health: string;
  last_scrape: string;
  last_error: string | null;
}

export interface MetricsTargetsResponse {
  status: string;
  targets: PrometheusTarget[];
}

export async function getMetricsSummary(): Promise<MetricsSummaryResponse> {
  return api.get("/metrics/summary");
}

export async function getMetricsTargets(): Promise<MetricsTargetsResponse> {
  return api.get("/metrics/targets");
}

// ---- Admin: Audit Logs ----

export interface AuditLogEntry {
  id: string;
  organization_id: string | null;
  actor_id: string | null;
  actor_type: string | null;
  action: string;
  resource_type: string;
  resource_id: string | null;
  details: Record<string, unknown>;
  ip_address: string | null;
  status_code: number | null;
  method: string | null;
  path: string | null;
  created_at: string;
}

export interface AuditLogListResponse {
  items: AuditLogEntry[];
  total: number;
  limit: number;
  offset: number;
}

export async function listAuditLogs(
  params?: {
    action?: string;
    actor_id?: string;
    actor_type?: string;
    resource_type?: string;
    resource_id?: string;
    status_code?: number;
    created_after?: string;
    created_before?: string;
    limit?: number;
    offset?: number;
  },
): Promise<AuditLogListResponse> {
  return api.get("/v1/admin/audit-logs", params as Record<string, string | number | boolean | undefined | null>);
}

export async function listGraphEdges(
  projectId: string,
  userId: string,
  params: {
    subject_id: string;
    predicate?: string | null;
    limit?: number;
    cursor?: string | null;
  },
): Promise<Schema["GraphEdgesListResponse"]> {
  return api.get(`/v1/projects/${projectId}/${userId}/graph/edges`, params as Record<string, string | number | boolean | undefined | null>);
}

// ---- Projects ----

export interface ProjectResponse {
  id: string;
  organization_id: string;
  name: string;
  description: string | null;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface MemberResponse {
  user_id: string;
  project_id: string;
  role: string;
  created_at: string;
}

export interface MemberListResponse {
  members: MemberResponse[];
  total: number;
}

export interface PaginatedResponse<T> {
  data: T[];
  next_cursor: string | null;
  has_more: boolean;
}

export async function listProjects(
  params?: { limit?: number; cursor?: string },
): Promise<PaginatedResponse<ProjectResponse>> {
  return api.get("/v1/projects", params as Record<string, string | number | boolean | undefined | null>);
}

export async function getProject(projectId: string): Promise<ProjectResponse> {
  return api.get(`/v1/projects/${projectId}`);
}

export async function createProject(name: string, description?: string): Promise<ProjectResponse> {
  return api.post("/v1/admin/projects", { name, description });
}

export async function updateProject(
  projectId: string,
  data: { name?: string; description?: string; is_active?: boolean },
): Promise<ProjectResponse> {
  return api.patch(`/v1/projects/${projectId}`, data);
}

export async function deleteProject(projectId: string): Promise<void> {
  return api.delete(`/v1/projects/${projectId}`);
}

export async function listProjectMembers(projectId: string): Promise<MemberListResponse> {
  return api.get(`/v1/projects/${projectId}/members`);
}

export async function addProjectMember(
  projectId: string,
  userId: string,
  role: string = "member",
): Promise<MemberResponse> {
  return api.post(`/v1/projects/${projectId}/members`, { user_id: userId, role });
}

export async function updateProjectMemberRole(
  projectId: string,
  userId: string,
  role: string,
): Promise<MemberResponse> {
  return api.patch(`/v1/projects/${projectId}/members/${userId}`, { role });
}

export async function removeProjectMember(projectId: string, userId: string): Promise<void> {
  return api.delete(`/v1/projects/${projectId}/members/${userId}`);
}
