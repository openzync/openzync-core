# TypeScript SDK Implementation Guide — `memgraph-ts`

> **Phase**: Phase 4 — Dashboard & SDKs (Week 10-12)
> **Priority**: P1
> **Package**: `memgraph-ts` on [npm](https://www.npmjs.com/package/memgraph-ts)
> **Source**: SRS §5.8.2

---

## 1. Package Overview

```
memgraph-ts/
├── src/
│   ├── index.ts                # Public exports
│   ├── client.ts               # MemGraph client class
│   ├── transport.ts            # HTTP transport — fetch-based, retry, auth
│   ├── errors.ts               # Typed error classes
│   ├── pagination.ts           # PaginatedAsyncIterator
│   ├── domains/
│   │   ├── memory.ts
│   │   ├── facts.ts
│   │   ├── graph.ts
│   │   ├── users.ts
│   │   └── sessions.ts
│   ├── models/
│   │   ├── memory.ts
│   │   ├── fact.ts
│   │   ├── graph.ts
│   │   ├── user.ts
│   │   └── session.ts
│   └── version.ts              # Auto-generated from package.json
├── tests/
│   ├── client.test.ts
│   ├── transport.test.ts
│   ├── pagination.test.ts
│   └── domains/
├── package.json
├── tsconfig.json
├── tsconfig.cjs.json           # CommonJS build config
├── README.md
└── LICENSE                     # Apache 2.0
```

---

## 2. Installation

```bash
npm install memgraph-ts
# or
yarn add memgraph-ts
# or
pnpm add memgraph-ts
```

Node.js 18+ (with native `fetch`) or modern browser (Chrome 90+, Firefox 90+, Safari 15+).

---

## 3. Runtime Strategy — Dual Platform

The SDK uses the **platform-native `fetch` API** — no `node-fetch` or `cross-fetch` dependency.

| Runtime | Transport |
|---|---|
| Node.js 18+ | `globalThis.fetch` (undici) |
| Browser | `window.fetch` |
| Edge / CF Workers | `globalThis.fetch` |

The SDK detects and throws a clear error if `fetch` is unavailable:

```typescript
// src/transport.ts
if (typeof globalThis.fetch === "undefined") {
  throw new Error(
    "memgraph-ts requires a fetch-compatible runtime (Node.js 18+, modern browser). " +
    "See https://github.com/thelinkai/memgraph-ts#requirements"
  );
}
```

### Dual module format

The package ships both **ESM** (`.mjs`) and **CommonJS** (`.cjs`) builds for maximum compatibility:

```json
{
  "type": "module",
  "main": "./dist/cjs/index.cjs",
  "module": "./dist/esm/index.mjs",
  "exports": {
    ".": {
      "import": "./dist/esm/index.mjs",
      "require": "./dist/cjs/index.cjs"
    }
  }
}
```

---

## 4. Client Constructor

```typescript
// src/client.ts
import { type TransportConfig, createTransport } from "./transport";
import { MemoryDomain } from "./domains/memory";
import { FactsDomain } from "./domains/facts";
import { GraphDomain } from "./domains/graph";
import { UsersDomain } from "./domains/users";
import { SessionsDomain } from "./domains/sessions";

export interface MemGraphConfig {
  /** API key (prefixed mg_live_ or mg_test_) */
  apiKey?: string;
  /** Base URL (without /v1). Defaults to env MEMGRAPH_BASE_URL or http://localhost:8000 */
  baseUrl?: string;
  /** Read timeout in ms. Default: 30000 */
  timeoutRead?: number;
  /** Write timeout in ms. Default: 60000 */
  timeoutWrite?: number;
  /** Max retries for retryable errors. Default: 3 */
  maxRetries?: number;
  /** Enable debug logging. Default: false */
  debug?: boolean;
}

export class MemGraph {
  public readonly memory: MemoryDomain;
  public readonly facts: FactsDomain;
  public readonly graph: GraphDomain;
  public readonly users: UsersDomain;
  public readonly sessions: SessionsDomain;

  constructor(config: MemGraphConfig = {}) {
    const apiKey = config.apiKey ?? getEnv("MEMGRAPH_API_KEY");
    const baseUrl = config.baseUrl ?? getEnv("MEMGRAPH_BASE_URL", "http://localhost:8000");
    const timeoutRead = config.timeoutRead ?? Number(getEnv("MEMGRAPH_TIMEOUT_READ", "30000"));
    const timeoutWrite = config.timeoutWrite ?? Number(getEnv("MEMGRAPH_TIMEOUT_WRITE", "60000"));
    const maxRetries = config.maxRetries ?? Number(getEnv("MEMGRAPH_MAX_RETRIES", "3"));
    const debug = config.debug ?? getEnv("MEMGRAPH_DEBUG", "").toLowerCase() === "true";

    if (!apiKey) {
      throw new MemGraphError(
        "API key is required. Pass apiKey to the constructor or set MEMGRAPH_API_KEY environment variable."
      );
    }

    const transport = createTransport({ apiKey, baseUrl, timeoutRead, timeoutWrite, maxRetries, debug });

    this.memory = new MemoryDomain(transport);
    this.facts = new FactsDomain(transport);
    this.graph = new GraphDomain(transport);
    this.users = new UsersDomain(transport);
    this.sessions = new SessionsDomain(transport);
  }
}

function getEnv(name: string, fallback?: string): string | undefined {
  // Works in Node.js (process.env) and Cloudflare Workers (globalThis env)
  if (typeof process !== "undefined" && process.env) {
    return process.env[name] ?? fallback;
  }
  return fallback;
}
```

---

## 5. HTTP Transport

```typescript
// src/transport.ts
import pkg from "../package.json";

export interface TransportConfig {
  apiKey: string;
  baseUrl: string;
  timeoutRead: number;
  timeoutWrite: number;
  maxRetries: number;
  debug: boolean;
}

export interface Transport {
  request<T>(method: string, path: string, options?: RequestOptions): Promise<T>;
}

export interface RequestOptions {
  json?: unknown;
  params?: Record<string, string | number | undefined>;
  timeoutRead?: number;
  timeoutWrite?: number;
}

export function createTransport(config: TransportConfig): Transport {
  const baseUrl = config.baseUrl.replace(/\/+$/, "") + "/v1";
  const userAgent = `memgraph-ts-sdk/${pkg.version}`;

  return {
    async request<T>(method: string, path: string, options?: RequestOptions): Promise<T> {
      const url = new URL(baseUrl + path);

      // Append query params
      if (options?.params) {
        for (const [key, value] of Object.entries(options.params)) {
          if (value !== undefined) {
            url.searchParams.set(key, String(value));
          }
        }
      }

      const headers: Record<string, string> = {
        Authorization: `Bearer ${config.apiKey}`,
        "User-Agent": userAgent,
        "Content-Type": "application/json",
      };

      const body = options?.json !== undefined ? JSON.stringify(options.json) : undefined;
      const readTimeout = options?.timeoutRead ?? config.timeoutRead;
      const writeTimeout = options?.timeoutWrite ?? config.timeoutWrite;

      // Build AbortController for timeout
      // We apply the stricter of read vs write based on request type
      const timeoutMs = method === "GET" || method === "DELETE" ? readTimeout : writeTimeout;
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

      let lastError: Error | null = null;

      for (let attempt = 0; attempt <= config.maxRetries; attempt++) {
        try {
          const response = await fetch(url.toString(), {
            method,
            headers,
            body,
            signal: controller.signal,
          });

          if (config.debug) {
            console.debug(`memgraph-ts: ${method} ${path} -> ${response.status} (attempt ${attempt + 1})`);
          }

          // Retry on 429 or 5xx
          if ((response.status === 429 || response.status >= 500) && attempt < config.maxRetries) {
            const retryAfter = parseRetryAfter(response);
            const wait = retryAfter ?? computeBackoff(attempt);
            if (config.debug) {
              console.debug(`memgraph-ts: retrying in ${wait}ms (status=${response.status})`);
            }
            await sleep(wait);
            continue;
          }

          // Non-retryable — map to typed error
          if (!response.ok) {
            await raiseOnError(response);
          }

          // Parse JSON body
          const text = await response.text();
          // 204 No Content or empty body
          if (!text) return undefined as T;
          return JSON.parse(text) as T;

        } catch (err) {
          if (err instanceof MemGraphError) throw err; // Already mapped

          if (err instanceof DOMException && err.name === "AbortError") {
            throw new TimeoutError(`Request timed out after ${timeoutMs}ms`);
          }

          // Network error — retry
          if (attempt < config.maxRetries) {
            const wait = computeBackoff(attempt);
            await sleep(wait);
            lastError = err as Error;
            continue;
          }

          throw new ConnectionError(`Failed to connect: ${(err as Error).message}`);
        } finally {
          clearTimeout(timeoutId);
        }
      }

      throw new MaxRetriesExceededError(
        `Request failed after ${config.maxRetries} retries`,
        { cause: lastError }
      );
    },
  };
}

function computeBackoff(attempt: number): number {
  const base = 1000; // 1 second
  const max = 30000; // 30 seconds
  const delay = Math.min(base * Math.pow(2, attempt), max);
  return delay + Math.random() * 500; // + jitter
}

function parseRetryAfter(response: Response): number | null {
  const header = response.headers.get("Retry-After");
  if (!header) return null;
  const seconds = parseInt(header, 10);
  return isNaN(seconds) ? null : seconds * 1000;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
```

---

## 6. Error Hierarchy

```typescript
// src/errors.ts

export class MemGraphError extends Error {
  public readonly statusCode?: number;
  public readonly requestId?: string;

  constructor(message: string, options?: { statusCode?: number; requestId?: string; cause?: Error }) {
    super(message, { cause: options?.cause });
    this.name = "MemGraphError";
    this.statusCode = options?.statusCode;
    this.requestId = options?.requestId;
  }
}

export class AuthenticationError extends MemGraphError {
  constructor(message?: string, opts?: { requestId?: string }) {
    super(message ?? "Authentication failed. Check your API key.", { statusCode: 401, ...opts });
    this.name = "AuthenticationError";
  }
}

export class NotFoundError extends MemGraphError {
  constructor(message?: string, opts?: { requestId?: string }) {
    super(message ?? "Resource not found.", { statusCode: 404, ...opts });
    this.name = "NotFoundError";
  }
}

export class RateLimitError extends MemGraphError {
  public readonly retryAfterSeconds?: number;

  constructor(message?: string, opts?: { requestId?: string; retryAfterSeconds?: number }) {
    super(message ?? "Rate limit exceeded.", { statusCode: 429, ...opts });
    this.name = "RateLimitError";
    this.retryAfterSeconds = opts?.retryAfterSeconds;
  }
}

export class ValidationError extends MemGraphError {
  constructor(message?: string, opts?: { requestId?: string }) {
    super(message ?? "Request validation failed.", { statusCode: 422, ...opts });
    this.name = "ValidationError";
  }
}

export class ServerError extends MemGraphError {
  constructor(message?: string, opts?: { requestId?: string }) {
    super(message ?? "Internal server error.", { statusCode: 500, ...opts });
    this.name = "ServerError";
  }
}

export class ConnectionError extends MemGraphError {
  constructor(message?: string, opts?: { cause?: Error }) {
    super(message ?? "Connection failed.", opts);
    this.name = "ConnectionError";
  }
}

export class TimeoutError extends MemGraphError {
  constructor(message?: string) {
    super(message ?? "Request timed out.");
    this.name = "TimeoutError";
  }
}

export class MaxRetriesExceededError extends MemGraphError {
  constructor(message?: string, opts?: { cause?: Error | null }) {
    super(message ?? "Request failed after exhausting retries.", opts);
    this.name = "MaxRetriesExceededError";
  }
}

async function raiseOnError(response: Response): Promise<never> {
  let body: Record<string, unknown> | undefined;
  try {
    body = await response.json() as Record<string, unknown>;
  } catch {
    // Response body not JSON — ignore
  }

  const errorPayload = (body?.error as Record<string, unknown>) ?? {};
  const message = errorPayload.message as string ?? response.statusText;
  const requestId = errorPayload.request_id as string | undefined;

  switch (response.status) {
    case 401:
      throw new AuthenticationError(message, { requestId });
    case 404:
      throw new NotFoundError(message, { requestId });
    case 429: {
      const retryAfter = parseRetryAfter(response);
      throw new RateLimitError(message, { requestId, retryAfterSeconds: retryAfter ?? undefined });
    }
    case 422:
      throw new ValidationError(message, { requestId });
    default:
      if (response.status >= 500) {
        throw new ServerError(message, { requestId });
      }
      throw new MemGraphError(message, { statusCode: response.status, requestId });
  }
}
```

---

## 7. Pagination

```typescript
// src/pagination.ts
export interface PageResponse<T> {
  data: T[];
  next_cursor?: string | null;
  has_more: boolean;
}

export class PaginatedAsyncIterator<T> implements AsyncIterable<T> {
  private fetcher: (cursor?: string) => Promise<PageResponse<T>>;
  private cursor?: string;
  private items: T[] = [];
  private index = 0;
  private exhausted = false;

  constructor(fetcher: (cursor?: string) => Promise<PageResponse<T>>) {
    this.fetcher = fetcher;
  }

  [Symbol.asyncIterator](): AsyncIterator<T> {
    return {
      next: async () => {
        if (this.index < this.items.length) {
          return { value: this.items[this.index++], done: false };
        }

        if (this.exhausted) {
          return { value: undefined, done: true };
        }

        const page = await this.fetcher(this.cursor);
        this.items = page.data;
        this.index = 0;

        if (!page.has_more || !page.data.length) {
          this.exhausted = true;
        }

        this.cursor = page.next_cursor ?? undefined;

        if (!this.items.length) {
          return { value: undefined, done: true };
        }

        this.index = 1;
        return { value: this.items[0], done: false };
      },
    };
  }
}
```

---

## 8. Typed Generics for Structured Extraction

The structured extraction endpoint lets users define their own JSON Schema. The SDK exposes this with generics:

```typescript
// src/domains/sessions.ts
export class SessionsDomain {
  // ...

  /**
   * Get structured extraction for a session.
   * Use generics to type the extracted data:
   *
   * ```typescript
   * interface OrderData {
   *   orderId: string;
   *   total: number;
   *   items: string[];
   * }
   * const data = await client.sessions.getExtract<OrderData>(userId, sessionId);
   * console.log(data.total);
   * ```
   */
  async getExtract<T = Record<string, unknown>>(
    userId: string,
    sessionId: string,
  ): Promise<ExtractionResponse<T>> {
    const response = await this.transport.request<ExtractionResponse<T>>(
      "GET",
      `/users/${userId}/sessions/${sessionId}/extract`,
    );
    return response;
  }
}

export interface ExtractionResponse<T> {
  id: string;
  session_id: string;
  data: T;
  created_at: string;
}
```

---

## 9. Domain Implementations

### 9.1 Memory Domain

```typescript
// src/domains/memory.ts
import { Transport, RequestOptions } from "../transport";
import { PaginatedAsyncIterator } from "../pagination";

export interface Message {
  role: "user" | "assistant" | "system" | "tool";
  content: string;
  created_at?: string;
  metadata?: Record<string, unknown>;
}

export interface MemoryIngestRequest {
  messages: Message[];
  session_id?: string;
}

export interface MemoryIngestResponse {
  job_id: string;
  status: string;
}

export interface ContextResponse {
  context: string;
}

export interface ContextJSONResponse {
  facts: Record<string, unknown>[];
  episodes: Record<string, unknown>[];
  entities: Record<string, unknown>[];
  communities: Record<string, unknown>[];
}

export class MemoryDomain {
  constructor(private transport: Transport) {}

  async ingest(
    userId: string,
    messages: (Message | Record<string, unknown>)[],
    sessionId?: string,
    options?: RequestOptions,
  ): Promise<MemoryIngestResponse> {
    const body: MemoryIngestRequest = {
      messages: messages as Message[],
    };
    if (sessionId) body.session_id = sessionId;

    return this.transport.request<MemoryIngestResponse>("POST", `/users/${userId}/memory`, {
      json: body,
      ...options,
    });
  }

  async getContext(
    userId: string,
    query: string,
    options?: { limit?: number; format?: "text" | "json" } & RequestOptions,
  ): Promise<string | ContextJSONResponse> {
    const response = await this.transport.request<unknown>(
      "GET",
      `/users/${userId}/context`,
      {
        params: {
          query,
          limit: String(options?.limit ?? 10),
          format: options?.format ?? "text",
        },
        ...options,
      },
    );

    if (options?.format === "json") {
      return response as ContextJSONResponse;
    }
    return response as string;
  }

  async delete(userId: string, options?: RequestOptions): Promise<void> {
    await this.transport.request("DELETE", `/users/${userId}/memory`, options);
  }
}
```

### 9.2 Facts Domain

```typescript
// src/domains/facts.ts
import { Transport, RequestOptions } from "../transport";
import { PaginatedAsyncIterator, PageResponse } from "../pagination";

export interface FactCreate {
  subject: string;
  predicate: string;
  object: string;
  valid_at?: string;
  expires_at?: string;
  [key: string]: unknown; // allow extra fields
}

export interface FactResponse {
  id: string;
  user_id: string;
  content: string;
  subject?: string;
  predicate?: string;
  object?: string;
  confidence: number;
  valid_from?: string;
  valid_to?: string;
  created_at: string;
}

export class FactsDomain {
  constructor(private transport: Transport) {}

  async add(
    userId: string,
    facts: (FactCreate | Record<string, unknown>)[],
    options?: RequestOptions,
  ): Promise<{ inserted: number }> {
    return this.transport.request<{ inserted: number }>("POST", `/users/${userId}/facts`, {
      json: { facts },
      ...options,
    });
  }

  async list(
    userId: string,
    options?: { limit?: number; cursor?: string } & RequestOptions,
  ): Promise<PageResponse<FactResponse>> {
    return this.transport.request<PageResponse<FactResponse>>(
      "GET",
      `/users/${userId}/facts`,
      {
        params: {
          limit: String(options?.limit ?? 50),
          cursor: options?.cursor,
        },
        ...options,
      },
    );
  }

  all(userId: string, limit = 50): PaginatedAsyncIterator<FactResponse> {
    return new PaginatedAsyncIterator((cursor) =>
      this.list(userId, { limit, cursor }),
    );
  }

  async delete(userId: string, factId: string, options?: RequestOptions): Promise<void> {
    await this.transport.request("DELETE", `/users/${userId}/facts/${factId}`, options);
  }
}
```

### 9.3 Graph Domain

```typescript
// src/domains/graph.ts
import { Transport, RequestOptions } from "../transport";

export interface GraphNode {
  uuid: string;
  name: string;
  type: string;
  summary?: string;
  created_at: string;
}

export interface GraphEdge {
  uuid: string;
  subject: string;
  predicate: string;
  object: string;
  fact?: string;
  valid_at?: string;
  invalid_at?: string;
}

export interface GraphSearchResult {
  id: string;
  type: "fact" | "episode" | "entity";
  content: string;
  score: number;
  metadata?: Record<string, unknown>;
}

export class GraphDomain {
  constructor(private transport: Transport) {}

  async nodes(
    userId: string,
    typeFilter?: string,
    options?: RequestOptions,
  ): Promise<GraphNode[]> {
    const response = await this.transport.request<{ data: GraphNode[] }>(
      "GET",
      `/users/${userId}/graph/nodes`,
      {
        params: { type: typeFilter },
        ...options,
      },
    );
    return response.data;
  }

  async getNode(userId: string, nodeId: string, options?: RequestOptions): Promise<GraphNode> {
    return this.transport.request<GraphNode>(
      "GET",
      `/users/${userId}/graph/nodes/${nodeId}`,
      options,
    );
  }

  async search(
    userId: string,
    query: string,
    types?: ("facts" | "episodes" | "entities")[],
    limit = 10,
    options?: RequestOptions,
  ): Promise<GraphSearchResult[]> {
    const response = await this.transport.request<{ data: GraphSearchResult[] }>(
      "GET",
      `/users/${userId}/search`,
      {
        params: {
          query,
          types: types?.join(","),
          limit: String(limit),
        },
        ...options,
      },
    );
    return response.data;
  }

  async edges(
    userId: string,
    filters?: { subject?: string; predicate?: string },
    options?: RequestOptions,
  ): Promise<GraphEdge[]> {
    const response = await this.transport.request<{ data: GraphEdge[] }>(
      "GET",
      `/users/${userId}/graph/edges`,
      {
        params: {
          subject: filters?.subject,
          predicate: filters?.predicate,
        },
        ...options,
      },
    );
    return response.data;
  }

  async communities(userId: string, options?: RequestOptions): Promise<Record<string, unknown>[]> {
    const response = await this.transport.request<{ data: Record<string, unknown>[] }>(
      "GET",
      `/users/${userId}/graph/communities`,
      options,
    );
    return response.data;
  }
}
```

### 9.4 Users Domain

```typescript
// src/domains/users.ts
import { Transport, RequestOptions } from "../transport";
import { PaginatedAsyncIterator, PageResponse } from "../pagination";

export interface UserCreateRequest {
  external_id: string;
  name?: string;
  email?: string;
  metadata?: Record<string, unknown>;
}

export interface UserResponse {
  id: string;
  external_id: string;
  name?: string;
  email?: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface UserUpdateRequest {
  name?: string;
  email?: string;
  metadata?: Record<string, unknown>;
}

export class UsersDomain {
  constructor(private transport: Transport) {}

  async create(
    externalId: string,
    name?: string,
    email?: string,
    metadata?: Record<string, unknown>,
    options?: RequestOptions,
  ): Promise<UserResponse> {
    const body: UserCreateRequest = { external_id: externalId };
    if (name !== undefined) body.name = name;
    if (email !== undefined) body.email = email;
    if (metadata !== undefined) body.metadata = metadata;

    return this.transport.request<UserResponse>("POST", "/users", {
      json: body,
      ...options,
    });
  }

  async get(userId: string, options?: RequestOptions): Promise<UserResponse> {
    return this.transport.request<UserResponse>("GET", `/users/${userId}`, options);
  }

  async update(
    userId: string,
    updates: UserUpdateRequest,
    options?: RequestOptions,
  ): Promise<UserResponse> {
    return this.transport.request<UserResponse>("PATCH", `/users/${userId}`, {
      json: updates,
      ...options,
    });
  }

  async list(
    options?: { limit?: number; cursor?: string } & RequestOptions,
  ): Promise<PageResponse<UserResponse>> {
    return this.transport.request<PageResponse<UserResponse>>("GET", "/users", {
      params: {
        limit: String(options?.limit ?? 50),
        cursor: options?.cursor,
      },
      ...options,
    });
  }

  all(limit = 50): PaginatedAsyncIterator<UserResponse> {
    return new PaginatedAsyncIterator((cursor) =>
      this.list({ limit, cursor }),
    );
  }

  async delete(userId: string, options?: RequestOptions): Promise<void> {
    await this.transport.request("DELETE", `/users/${userId}`, options);
  }
}
```

### 9.5 Sessions Domain

```typescript
// src/domains/sessions.ts
import { Transport, RequestOptions } from "../transport";
import { PaginatedAsyncIterator, PageResponse } from "../pagination";
import type { ExtractionResponse } from "./extraction";

export interface SessionCreateRequest {
  external_id?: string;
  metadata?: Record<string, unknown>;
}

export interface SessionResponse {
  id: string;
  user_id: string;
  external_id?: string;
  metadata: Record<string, unknown>;
  created_at: string;
  closed_at?: string;
}

export interface MessageResponse {
  id: string;
  role: string;
  content: string;
  metadata?: Record<string, unknown>;
  created_at: string;
}

export class SessionsDomain {
  constructor(private transport: Transport) {}

  async create(
    userId: string,
    externalId?: string,
    metadata?: Record<string, unknown>,
    options?: RequestOptions,
  ): Promise<SessionResponse> {
    const body: SessionCreateRequest = {};
    if (externalId !== undefined) body.external_id = externalId;
    if (metadata !== undefined) body.metadata = metadata;

    return this.transport.request<SessionResponse>("POST", `/users/${userId}/sessions`, {
      json: body,
      ...options,
    });
  }

  async list(
    userId: string,
    options?: { limit?: number; cursor?: string } & RequestOptions,
  ): Promise<PageResponse<SessionResponse>> {
    return this.transport.request<PageResponse<SessionResponse>>(
      "GET",
      `/users/${userId}/sessions`,
      {
        params: {
          limit: String(options?.limit ?? 50),
          cursor: options?.cursor,
        },
        ...options,
      },
    );
  }

  all(userId: string, limit = 50): PaginatedAsyncIterator<SessionResponse> {
    return new PaginatedAsyncIterator((cursor) =>
      this.list(userId, { limit, cursor }),
    );
  }

  async get(
    userId: string,
    sessionId: string,
    options?: RequestOptions,
  ): Promise<SessionResponse> {
    return this.transport.request<SessionResponse>(
      "GET",
      `/users/${userId}/sessions/${sessionId}`,
      options,
    );
  }

  async getMessages(
    userId: string,
    sessionId: string,
    options?: { limit?: number; cursor?: string } & RequestOptions,
  ): Promise<PageResponse<MessageResponse>> {
    return this.transport.request<PageResponse<MessageResponse>>(
      "GET",
      `/users/${userId}/sessions/${sessionId}/messages`,
      {
        params: {
          limit: String(options?.limit ?? 50),
          cursor: options?.cursor,
        },
        ...options,
      },
    );
  }

  async delete(
    userId: string,
    sessionId: string,
    options?: RequestOptions,
  ): Promise<void> {
    await this.transport.request("DELETE", `/users/${userId}/sessions/${sessionId}`, options);
  }
}
```

---

## 10. Public Exports

```typescript
// src/index.ts
export { MemGraph } from "./client";
export type { MemGraphConfig } from "./client";

export {
  MemGraphError,
  AuthenticationError,
  NotFoundError,
  RateLimitError,
  ValidationError,
  ServerError,
  ConnectionError,
  TimeoutError,
  MaxRetriesExceededError,
} from "./errors";

export { PaginatedAsyncIterator } from "./pagination";
export type { PageResponse } from "./pagination";

// Domain classes
export { MemoryDomain } from "./domains/memory";
export { FactsDomain } from "./domains/facts";
export { GraphDomain } from "./domains/graph";
export { UsersDomain } from "./domains/users";
export { SessionsDomain } from "./domains/sessions";

// Types
export type {
  Message,
  MemoryIngestRequest,
  MemoryIngestResponse,
  ContextResponse,
  ContextJSONResponse,
} from "./domains/memory";

export type { FactCreate, FactResponse } from "./domains/facts";

export type { GraphNode, GraphEdge, GraphSearchResult } from "./domains/graph";

export type {
  UserCreateRequest,
  UserResponse,
  UserUpdateRequest,
} from "./domains/users";

export type {
  SessionCreateRequest,
  SessionResponse,
  MessageResponse,
} from "./domains/sessions";

export type { ExtractionResponse } from "./domains/sessions";
```

---

## 11. Build Configuration

### tsconfig.json (ESM)

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "bundler",
    "declaration": true,
    "declarationMap": true,
    "sourceMap": true,
    "outDir": "./dist/esm",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "forceConsistentCasingInFileNames": true
  },
  "include": ["src"],
  "exclude": ["node_modules", "tests"]
}
```

### tsconfig.cjs.json

```json
{
  "extends": "./tsconfig.json",
  "compilerOptions": {
    "module": "CommonJS",
    "moduleResolution": "node",
    "outDir": "./dist/cjs"
  }
}
```

### package.json

```json
{
  "name": "memgraph-ts",
  "version": "1.0.0",
  "description": "MemGraph — open-source agent memory platform TypeScript SDK",
  "author": "TheLinkAI <engineering@thelinkai.com>",
  "license": "Apache-2.0",
  "repository": {
    "type": "git",
    "url": "https://github.com/thelinkai/memgraph-ts.git"
  },
  "keywords": ["memgraph", "agent-memory", "knowledge-graph", "llm", "ai"],
  "type": "module",
  "main": "./dist/cjs/index.cjs",
  "module": "./dist/esm/index.mjs",
  "types": "./dist/esm/index.d.ts",
  "exports": {
    ".": {
      "import": "./dist/esm/index.mjs",
      "require": "./dist/cjs/index.cjs",
      "types": "./dist/esm/index.d.ts"
    }
  },
  "files": ["dist", "README.md", "LICENSE"],
  "scripts": {
    "build": "npm run build:esm && npm run build:cjs",
    "build:esm": "tsc -p tsconfig.json && mv dist/esm/index.js dist/esm/index.mjs",
    "build:cjs": "tsc -p tsconfig.cjs.json && mv dist/cjs/index.js dist/cjs/index.cjs",
    "test": "vitest run",
    "test:watch": "vitest",
    "lint": "eslint src/",
    "prepublishOnly": "npm run build && npm test"
  },
  "devDependencies": {
    "typescript": "^5.4",
    "vitest": "^1.6",
    "eslint": "^8.57"
  }
}
```

---

## 12. npm Publishing CI

```yaml
# .gitlab-ci.yml (in memgraph-ts repo)
publish-npm:
  stage: release
  image: node:20-alpine
  only:
    - tags
  script:
    - npm ci
    - npm run build
    - npm publish --access public
  variables:
    NPM_TOKEN: ${NPM_TOKEN}
```

---

## 13. CORS Warning — Browser Usage

When using the SDK from a browser, CORS headers must be configured on the MemGraph server. **Never expose a master tenant API key in browser code.** Use short-lived, scoped keys intended for browser clients.

Document prominently in README:

```
## Browser Usage

memgraph-ts works in modern browsers, but:

1. ⚠️ **Never embed your master API key in client-side code.**
   Generate short-lived, read-only scoped keys for browser usage.
   See [API Key Scopes](#) in the server admin docs.

2. The MemGraph server must be configured with your browser origin
   in the `CORS_ORIGINS` environment variable.

3. For server-side usage (Node.js 18+), no additional setup is needed.
```

---

## 14. MCP Server Note

The MCP server (SSE transport) is a **Node.js-only** feature. Document in the README:

```
## MCP Integration

The MemGraph MCP server uses SSE transport and is Node.js-only.
See [github.com/thelinkai/memgraph-mcp](https://github.com/thelinkai/memgraph-mcp)
for setup instructions.

The `memgraph-ts` SDK is not required for MCP — the MCP server communicates
with the MemGraph API directly.
```

---

## 15. Tree-Shakeable Exports

The SDK exports individual named classes and functions rather than a single namespace object. This ensures bundlers (webpack, Rollup, esbuild, Vite) can tree-shake unused code:

```typescript
// Good — tree-shakeable
import { MemGraph, NotFoundError } from "memgraph-ts";
import { PaginatedAsyncIterator } from "memgraph-ts";

// Not recommended
import * as MemGraph from "memgraph-ts";
```

---

## 16. Complete Usage Example

```typescript
import { MemGraph } from "memgraph-ts";

async function main() {
  const client = new MemGraph({
    apiKey: "mg_live_abc123",
    baseUrl: "https://mg.example.com",
  });

  // 1. Create a user
  const user = await client.users.create("usr_001", "Alice", "alice@example.com");
  console.log(`User: ${user.id}`);

  // 2. Create a session
  const session = await client.sessions.create(user.id, "sess_001");
  console.log(`Session: ${session.id}`);

  // 3. Ingest memory
  const ingest = await client.memory.ingest(user.id, [
    { role: "user", content: "I prefer Python over JavaScript." },
    { role: "assistant", content: "Noted!" },
  ], session.id);
  console.log(`Ingested: ${ingest.job_id}`);

  // 4. Get context
  const context = await client.memory.getContext(user.id, "programming preferences");
  console.log(`Context: ${context}`);

  // 5. Search graph
  const results = await client.graph.search(user.id, "preferences", ["facts", "entities"]);
  for (const r of results) {
    console.log(`  ${r.type}: ${r.content} (score: ${r.score})`);
  }

  // 6. Add business facts
  await client.facts.add(user.id, [
    { subject: user.id, predicate: "purchased", object: "Pro plan" },
  ]);

  // 7. List facts with paginated iterator
  for await (const fact of client.facts.all(user.id)) {
    console.log(`Fact: ${fact.content}`);
  }

  // 8. List users with paginated iterator
  for await (const u of client.users.all()) {
    console.log(`User: ${u.name ?? u.external_id}`);
  }
}

main().catch(console.error);
```

---

## 17. Testing

```typescript
// tests/client.test.ts
import { describe, it, expect, vi, beforeEach } from "vitest";
import { MemGraph } from "../src";

describe("MemGraph client", () => {
  it("throws on missing API key", () => {
    expect(() => new MemGraph({} as any)).toThrow("API key is required");
  });

  it("uses env MEMGRAPH_API_KEY", () => {
    vi.stubEnv("MEMGRAPH_API_KEY", "mg_test_envkey");
    const client = new MemGraph({ baseUrl: "http://test.local" });
    expect(client).toBeDefined();
    vi.unstubAllEnvs();
  });

  it("sets auth and user-agent headers", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ data: [] }), { status: 200 })
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = new MemGraph({ apiKey: "mg_test_key", baseUrl: "http://test.local" });
    await client.users.list();

    const request = fetchMock.mock.calls[0][0] as Request;
    expect(request.headers.get("Authorization")).toBe("Bearer mg_test_key");
    expect(request.headers.get("User-Agent")).toMatch(/^memgraph-ts-sdk\//);

    vi.unstubAllGlobals();
  });
});
```

Run tests:

```bash
npm test
# or
npx vitest run
```
