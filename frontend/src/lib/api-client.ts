// ═══════════════════════════════════════════════════════════════════════════════
// OpenZep — Centralized API Client
// ═══════════════════════════════════════════════════════════════════════════════
//
// Every API call in the frontend goes through this module.
//   - Reads NEXT_PUBLIC_API_URL from env (fallback: http://localhost:8000)
//   - Injects Authorization header from sessionStorage
//   - Handles 401 → redirect to login
//   - Provides typed request helpers so every page gets consistent error handling
//
// Never hardcode the base URL in a page file again.
// ═══════════════════════════════════════════════════════════════════════════════

const API_BASE: string =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// ─── Auth helpers ─────────────────────────────────────────────────────────────

function getAccessToken(): string | null {
  if (typeof window === "undefined") return null;
  return sessionStorage.getItem("mg_access_token");
}

function getRefreshToken(): string | null {
  if (typeof window === "undefined") return null;
  return sessionStorage.getItem("mg_refresh_token");
}

function clearTokens(): void {
  sessionStorage.removeItem("mg_access_token");
  sessionStorage.removeItem("mg_refresh_token");
}

function getAuthHeaders(): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  const token = getAccessToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  return headers;
}

// ─── Response types ───────────────────────────────────────────────────────────

export interface PaginatedResponse<T> {
  data: T[];
  next_cursor?: string | null;
  has_more?: boolean;
  total?: number;
  items?: T[];
}

// ─── Error class ──────────────────────────────────────────────────────────────

export class ApiError extends Error {
  status: number;
  body: unknown;

  constructor(message: string, status: number, body: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }

  get isUnauthorized(): boolean {
    return this.status === 401;
  }

  get isNotFound(): boolean {
    return this.status === 404;
  }

  get isRateLimited(): boolean {
    return this.status === 429;
  }

  get isServerError(): boolean {
    return this.status >= 500;
  }
}

// ─── Core request helper ──────────────────────────────────────────────────────

async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const url = `${API_BASE}${path}`;
  const headers: Record<string, string> = {
    ...getAuthHeaders(),
    ...(options.headers as Record<string, string>),
  };

  const res = await fetch(url, {
    ...options,
    headers,
  });

  // 401 → tokens expired, redirect to login
  if (res.status === 401) {
    clearTokens();
    if (typeof window !== "undefined") {
      window.location.href = "/login?reason=not-signed-in";
    }
    throw new ApiError("Unauthorized", 401, null);
  }

  // No content (204) — return empty
  if (res.status === 204) {
    return {} as T;
  }

  // Try to parse JSON body
  let body: unknown;
  try {
    body = await res.json();
  } catch {
    body = null;
  }

  if (!res.ok) {
    const message =
      (body as { message?: string })?.message ??
      (body as { detail?: string })?.detail ??
      `Request failed with status ${res.status}`;
    throw new ApiError(message, res.status, body);
  }

  return body as T;
}

// ─── Typed helpers ────────────────────────────────────────────────────────────

/** GET request */
export function get<T>(path: string): Promise<T> {
  return request<T>(path, { method: "GET" });
}

/** POST request */
export function post<T>(path: string, data?: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    body: data !== undefined ? JSON.stringify(data) : undefined,
  });
}

/** PUT request */
export function put<T>(path: string, data?: unknown): Promise<T> {
  return request<T>(path, {
    method: "PUT",
    body: data !== undefined ? JSON.stringify(data) : undefined,
  });
}

/** PATCH request */
export function patch<T>(path: string, data?: unknown): Promise<T> {
  return request<T>(path, {
    method: "PATCH",
    body: data !== undefined ? JSON.stringify(data) : undefined,
  });
}

/** DELETE request */
export function del<T = void>(path: string): Promise<T> {
  return request<T>(path, { method: "DELETE" });
}

// ─── Pagination helpers ───────────────────────────────────────────────────────

export interface CursorPageParams {
  limit?: number;
  cursor?: string;
}

export interface OffsetPageParams {
  limit?: number;
  offset?: number;
}

/** Normalise API responses that might use `data`, `items`, or be a bare array. */
export function extractList<T>(response: unknown): T[] {
  if (Array.isArray(response)) return response as T[];
  const obj = response as Record<string, unknown>;
  if (Array.isArray(obj.data)) return obj.data as T[];
  if (Array.isArray(obj.items)) return obj.items as T[];
  return [];
}

// ─── Re-export base URL for edge cases ───────────────────────────────────────

export { API_BASE, getAccessToken, clearTokens };
