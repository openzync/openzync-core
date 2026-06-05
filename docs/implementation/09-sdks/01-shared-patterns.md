# Shared SDK Patterns

> **Phase**: Phase 2 (SDK P0 — Python), Phase 4 (TypeScript), Phase 5 (Go)
> **Priority**: P0 (Python), P1 (TypeScript), P2 (Go)
> **Source**: SRS §5.8, §8.2–8.5

This document defines the **shared contract** that all three official OpenZep SDKs (`OpenZep-py`, `OpenZep-ts`, `OpenZep-go`) MUST implement. Every SDK should be derivable from this document + its language-specific guide + the OpenAPI spec.

---

## 1. Client Constructor

Every SDK exposes a single `OpenZep` client class. The constructor accepts:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `api_key` | `string` | env `MEMGRAPH_API_KEY` | API key, prefixed `mg_live_` or `mg_test_` |
| `base_url` | `string` | env `MEMGRAPH_BASE_URL` or `http://localhost:8000` | OpenZep API base URL (without `/v1`) |
| `timeout_read` | `int` | `30` | Read timeout in seconds |
| `timeout_write` | `int` | `60` | Write timeout in seconds |
| `max_retries` | `int` | `3` | Maximum retry attempts for retryable errors |
| `debug` | `bool` | `false` | Enable debug logging |

**Resolution order**: constructor arg → env variable → default.

```python
# Python
client = OpenZep(api_key="mg_live_abc123")
```

```typescript
// TypeScript
const client = new OpenZep({ apiKey: "mg_live_abc123" });
```

```go
// Go
client, err := OpenZep.NewClient(
    OpenZep.WithAPIKey("mg_live_abc123"),
    OpenZep.WithBaseURL("https://mg.example.com"),
)
```

If neither `api_key` nor `MEMGRAPH_API_KEY` is set, the constructor MUST raise/throw a clear error at construction time — not on the first API call.

---

## 2. Authentication

All requests include:

```
Authorization: Bearer <api_key>
```

**Rules**:
- The SDK MUST set this header on every outbound request.
- Auto-refresh is NOT required. API keys are long-lived and do not expire with a refresh token flow.
- If the server returns 401, the SDK MUST surface that as an `AuthenticationError` — no retry.

---

## 3. User-Agent Header

Every request MUST include:

```
User-Agent: OpenZep-{lang}-sdk/{version}
```

| SDK | Header value |
|---|---|
| Python | `OpenZep-py-sdk/1.0.0` |
| TypeScript | `OpenZep-ts-sdk/1.0.0` |
| Go | `OpenZep-go-sdk/1.0.0` |

The version MUST be derived from the package version at build time (not hardcoded).

---

## 4. Retry Behaviour

### 4.1 Retryable status codes

| Code | Condition | Retry |
|---|---|---|
| `429` | Rate limit exceeded | ✅ Yes |
| `5xx` | Server error (500, 502, 503, 504) | ✅ Yes |
| `4xx` | All client errors (400, 401, 403, 404, 409, 422) | ❌ No |

### 4.2 Algorithm — exponential backoff with jitter

```
attempt = 0
while attempt < max_retries:
    response = await send_request()
    if response.status not in [429, 5xx]:
        return response
    attempt += 1
    wait = min(base_delay * 2^attempt, max_delay) + random_jitter(0, 0.5 * base)
    sleep(wait)
raise MaxRetriesExceededError(...)
```

| Parameter | Default |
|---|---|
| `base_delay` | 1.0 second |
| `max_delay` | 30.0 seconds |
| `jitter` | Random 0–500ms added to each wait |

### 4.3 Retry headers

If the server sends a `Retry-After` header, the SDK MUST honour it (use its value instead of the computed backoff).

### 4.4 Idempotency caveat

Write operations (`POST`, `PATCH`, `DELETE`) may be retried. The SDK MUST document that retries are safe because the API guarantees idempotency on write endpoints (via `user_id` scoping and cursor-based dedup). Read operations (`GET`) are always safe to retry.

---

## 5. Timeouts

Two independent timeout values:

| Timeout | Default | Applied to |
|---|---|---|
| `timeout_read` | 30s | Response body read (TTFB + download) |
| `timeout_write` | 60s | Request body upload |

The SDK MUST accept per-call timeout overrides. Example patterns:

```python
# Per-call override
await client.memory.ingest(..., timeout_read=10.0)  # 10 second timeout for this call only
```

```typescript
// Per-call override
await client.memory.ingest(..., { timeoutRead: 10_000 });
```

```go
// Per-call override via context
ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
defer cancel()
resp, err := client.AddMemory(ctx, userID, req)
```

---

## 6. Error Handling

HTTP errors MUST be translated into typed exceptions/errors.

### 6.1 Exception hierarchy

```
MemGraphError (base)
├── AuthenticationError    (401)
├── NotFoundError          (404)
├── RateLimitError         (429)
├── ValidationError        (422)
└── ServerError            (500+)
```

### 6.2 All errors carry:

| Field | Description |
|---|---|
| `message` | Human-readable error description |
| `status_code` | HTTP status code that caused the error |
| `request_id` | `request_id` from the error response body (if available) |
| `response` | The raw response body (if applicable, for debugging) |

### 6.3 Non-HTTP errors

| Error | Condition |
|---|---|
| `ConnectionError` | DNS resolution failure, connection refused |
| `TimeoutError` | Request exceeded the configured timeout |

### 6.4 Mapping logic

```python
def _raise_on_error(response):
    if response.status_code == 401:
        raise AuthenticationError(...)
    if response.status_code == 404:
        raise NotFoundError(...)
    if response.status_code == 429:
        raise RateLimitError(...)
    if response.status_code == 422:
        raise ValidationError(...)
    if response.status_code >= 500:
        raise ServerError(...)
    # 2xx — no-op
```

---

## 7. Pagination

All list endpoints return cursor-based pagination. The SDK MUST provide an **iterable abstraction** that auto-fetches subsequent pages.

### 7.1 Wire format

```json
{
  "data": [...],
  "next_cursor": "c_abc123",
  "has_more": true
}
```

### 7.2 SDK pattern

```python
# Python — iterate without thinking about pages
for user in client.users.list():
    print(user.name)

# Equivalent manual pagination
page = await client.users.list(limit=50)
for user in page.data:
    ...
while page.has_more:
    page = await client.users.list(limit=50, cursor=page.next_cursor)
```

```typescript
// TypeScript
for await (const user of client.users.list()) {
    console.log(user.name);
}
```

```go
// Go — callback-based iteration
err := client.ListUsers(ctx, func(u *User) error {
    fmt.Println(u.Name)
    return nil
})
```

### 7.3 Implementation contract

The pagination helper MUST:
1. Yield items from the current page.
2. When exhausted, fetch the next page using `next_cursor`.
3. Stop when `has_more` is `false` or the next page returns empty.
4. Accept an optional `limit` parameter (default 50, max 200).
5. Be lazy — do NOT pre-fetch all pages.

---

## 8. Logging

The SDK MUST support optional debug logging via the host language's standard logging library.

| SDK | Library |
|---|---|
| Python | `logging` (stdlib) |
| TypeScript | `console.debug` or optional `debug` package |
| Go | `log/slog` (Go 1.21+) or `log` |

```python
# Python — enabled via constructor
client = OpenZep(api_key="...", debug=True)

# Or via stdlib logger
import logging
logging.getLogger("OpenZep").setLevel(logging.DEBUG)
```

```typescript
// TypeScript — enabled via constructor
const client = new OpenZep({ apiKey: "...", debug: true });
```

```go
// Go — via option
client, err := OpenZep.NewClient(
    OpenZep.WithAPIKey("..."),
    OpenZep.WithLogger(slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelDebug}))),
)
```

Debug logs MUST include:
- Request method, path, and headers (with `Authorization` and `api_key` **redacted**).
- Response status code and latency.
- Retry attempts and backoff timing.

---

## 9. API Domain Structure

All SDKs organize methods into nested domain objects for discoverability:

| Domain | Methods |
|---|---|
| `client.memory` | `ingest`, `get_context`, `delete` |
| `client.facts` | `add`, `list`, `delete` |
| `client.graph` | `nodes`, `get_node`, `search`, `edges`, `communities` |
| `client.users` | `create`, `get`, `update`, `list`, `delete` |
| `client.sessions` | `create`, `list`, `get`, `get_messages`, `delete` |

```python
client.memory.ingest(user_id="u1", messages=[...])
client.facts.add(user_id="u1", facts=[...])
client.graph.search(user_id="u1", query="preferences")
client.users.create(external_id="ext_1", name="Alice")
client.sessions.list(user_id="u1")
```

---

## 10. Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MEMGRAPH_API_KEY` | — | API key |
| `MEMGRAPH_BASE_URL` | `http://localhost:8000` | Base URL (without `/v1`) |
| `MEMGRAPH_TIMEOUT_READ` | `30` | Read timeout in seconds |
| `MEMGRAPH_TIMEOUT_WRITE` | `60` | Write timeout in seconds |
| `MEMGRAPH_MAX_RETRIES` | `3` | Maximum retries |
| `MEMGRAPH_DEBUG` | `false` | Enable debug logging |

---

## 11. Base URL Handling

The SDK MUST append `/v1` to the base URL automatically. The user provides `https://mg.example.com` and the SDK calls `https://mg.example.com/v1/users/{user_id}/memory`.

This keeps migration to future API versions clean — when `/v2` is introduced, only the base URL or a constructor argument changes.

---

## 12. Partial Response Forward Compatibility

If the OpenZep API adds new fields to responses, existing SDK versions MUST NOT break.

| SDK | Mechanism |
|---|---|
| Python | `model_config = ConfigDict(extra='ignore')` on all Pydantic response models |
| TypeScript | Spread unknown properties or use `zod`'s `.passthrough()` |
| Go | Use `json.RawMessage` for unknown fields or ignore unknown JSON keys (`json:"-"`) |

The SDK MUST parse only the fields it knows about and silently ignore new or undocumented fields.

---

## 13. Rate Limit Awareness

When a `RateLimitError` (429) is received:
1. The SDK's retry logic handles it automatically (up to `max_retries`).
2. If retries are exhausted, `RateLimitError` is surfaced to the caller with the `Retry-After` value (if present) exposed as `.retry_after_seconds`.

```python
try:
    await client.memory.ingest(...)
except RateLimitError as e:
    print(f"Rate limited. Retry after {e.retry_after_seconds}s")
```

---

## 14. Async / Sync Convention

| SDK | Primary | Wrapper |
|---|---|---|
| Python | `async` (asyncio) | Sync wrapper using `asyncio.run()` |
| TypeScript | `async` (Promise) | natively `async` — no sync wrapper |
| Go | `sync` (goroutines) | natively sync — callers manage concurrency |

---

## 15. Testing the Shared Contract

Each SDK MUST include a **shared contract test suite** that verifies:

1. **Constructor**: rejects missing API key, accepts env vars, accepts explicit args.
2. **Auth header**: every outgoing request carries `Authorization: Bearer <key>`.
3. **User-Agent**: header is correctly formatted.
4. **Retry**: 429 gets retried `max_retries` times, 400 does NOT get retried.
5. **Timeout**: requests that exceed the timeout raise `TimeoutError`.
6. **Pagination**: iterating 3 pages of mock data yields all items without error.
7. **Error mapping**: every HTTP error code maps to the correct typed exception.
8. **Forward compat**: response with extra unknown fields does not cause a parse error.

Use a mock HTTP server (e.g., `responses` in Python, `nock` in TS, `httptest` in Go) to simulate server behaviour.

---

## 16. CI Integration

All SDKs MUST run the shared contract tests as part of CI on every push to `main` and on every tagged release.

| Check | Command |
|---|---|
| Python | `pytest tests/ -v` |
| TypeScript | `npm test` |
| Go | `go test ./... -v` |
