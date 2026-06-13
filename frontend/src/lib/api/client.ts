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
  userId: string,
  params?: {
    limit?: number;
    cursor?: string | null;
    include_closed?: boolean;
  },
): Promise<Schema["PaginatedResponse_SessionListResponse_"]> {
  return api.get(`/v1/users/${userId}/sessions`, params as Record<string, string | number | boolean | undefined | null>);
}

export async function createSession(
  userId: string,
  payload: Schema["CreateSessionRequest"],
): Promise<Schema["SessionResponse"]> {
  return api.post(`/v1/users/${userId}/sessions`, payload);
}

export async function getSession(
  userId: string,
  sessionId: string,
): Promise<Schema["SessionResponse"]> {
  return api.get(`/v1/users/${userId}/sessions/${sessionId}`);
}

export async function deleteSession(userId: string, sessionId: string): Promise<void> {
  return api.delete(`/v1/users/${userId}/sessions/${sessionId}`);
}

export async function getSessionMessages(
  userId: string,
  sessionId: string,
  params?: {
    limit?: number;
    cursor?: string | null;
  },
): Promise<Schema["PaginatedResponse_MessageResponse_"]> {
  return api.get(`/v1/users/${userId}/sessions/${sessionId}/messages`, params as Record<string, string | number | boolean | undefined | null>);
}

export async function getSessionFacts(
  userId: string,
  sessionId: string,
  params?: {
    limit?: number;
    cursor?: string | null;
  },
): Promise<Schema["PaginatedResponse_FactResponse_"]> {
  return api.get(`/v1/users/${userId}/sessions/${sessionId}/facts`, params as Record<string, string | number | boolean | undefined | null>);
}

// ---- Graph ----

export async function listGraphNodes(
  userId: string,
  params?: {
    limit?: number;
    cursor?: string | null;
    entity_type?: string | null;
  },
): Promise<Schema["GraphNodesListResponse"]> {
  return api.get(`/v1/users/${userId}/graph/nodes`, params as Record<string, string | number | boolean | undefined | null>);
}

export async function getGraphNode(
  userId: string,
  nodeId: string,
): Promise<Schema["GraphNodeDetailResponse"]> {
  return api.get(`/v1/users/${userId}/graph/nodes/${nodeId}`);
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

export async function listGraphEdges(
  userId: string,
  params: {
    subject_id: string;
    predicate?: string | null;
    limit?: number;
    cursor?: string | null;
  },
): Promise<Schema["GraphEdgesListResponse"]> {
  return api.get(`/v1/users/${userId}/graph/edges`, params as Record<string, string | number | boolean | undefined | null>);
}
