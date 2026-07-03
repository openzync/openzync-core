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

/** Parse response JSON or throw a structured error with status and preview. */
export async function safeJsonParse<T>(response: Response): Promise<T> {
  const text = await response.text();
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new Error(
      `Failed to parse response (${response.status}): ${text.slice(0, 200)}`,
    );
  }
}

/** Track if we are already refreshing to avoid infinite loops. */
let _refreshing = false;

// ─── Auth helpers ─────────────────────────────────────────────────────────────

function getAccessToken(): string | null {
  if (typeof window === "undefined") return null;
  return sessionStorage.getItem("mg_access_token");
}

function getRefreshToken(): string | null {
  if (typeof window === "undefined") return null;
  return sessionStorage.getItem("mg_refresh_token");
}

function storeTokens(access: string, refresh: string): void {
  sessionStorage.setItem("mg_access_token", access);
  sessionStorage.setItem("mg_refresh_token", refresh);
}

function clearTokens(): void {
  sessionStorage.removeItem("mg_access_token");
  sessionStorage.removeItem("mg_refresh_token");
}

/**
 * Attempt to exchange a refresh token for a new access token.
 * Returns the new access token, or null if the refresh fails.
 */
async function refreshAccessToken(): Promise<string | null> {
  const refreshToken = getRefreshToken();
  if (!refreshToken) return null;

  try {
    const res = await fetch(`${API_BASE}/api/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });

    if (!res.ok) {
      // Refresh failed (e.g. expired or revoked) — force re-login.
      clearTokens();
      return null;
    }

    const body = (await res.json()) as {
      access_token: string;
      refresh_token?: string;
    };

    // Store the new pair (the backend may also issue a new refresh token).
    storeTokens(body.access_token, body.refresh_token ?? refreshToken);
    return body.access_token;
  } catch {
    // Network error during refresh — do not clear tokens, caller may retry.
    return null;
  }
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

  let res = await fetch(url, {
    ...options,
    headers,
  });

  // 401 → attempt silent token refresh, then retry once
  if (res.status === 401 && !_refreshing) {
    _refreshing = true;
    try {
      const newToken = await refreshAccessToken();
      if (newToken) {
        // Retry the original request with the fresh token.
        headers["Authorization"] = `Bearer ${newToken}`;
        res = await fetch(url, {
          ...options,
          headers,
        });
      } else {
        // Refresh failed — nothing more to try.
        clearTokens();
        if (typeof window !== "undefined") {
          window.location.href = "/login?reason=not-signed-in";
        }
        throw new ApiError("Unauthorized", 401, null);
      }
    } finally {
      _refreshing = false;
    }
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
