> **⚠️ POSTPONED TO v1.1+** — This SDK is not in the v1.0 scope. Python SDK (`memgraph-py`) is the only SDK for v1.0. This document remains as a reference for future implementation.

---

# Go SDK Implementation Guide — `OpenZep-go`

> **Phase**: Phase 5 — Hardening (Week 13-14)
> **Priority**: P2
> **Package**: `github.com/thelinkAI/OpenZep-go` on [pkg.go.dev](https://pkg.go.dev/github.com/thelinkAI/OpenZep-go)
> **Source**: SRS §5.8.3

---

## 1. Package Overview

```
OpenZep-go/
├── client.go              # Client interface + implementation
├── client_test.go         # Test suite with httptest
├── transport.go           # HTTP transport with retry + auth
├── transport_test.go
├── errors.go              # Sentinel error vars
├── pagination.go          # Page + iter helper
├── memory.go              # Memory domain
├── memory_test.go
├── facts.go               # Facts domain
├── facts_test.go
├── graph.go               # Graph domain
├── graph_test.go
├── users.go               # Users domain
├── users_test.go
├── sessions.go            # Sessions domain
├── sessions_test.go
├── options.go             # Functional options for NewClient
├── go.mod
├── go.sum
├── README.md
└── LICENSE                # Apache 2.0
```

---

## 2. Module Setup

```go
// go.mod
module github.com/thelinkAI/OpenZep-go

go 1.22

require (
    golang.org/x/time v0.5.0 // for rate.Limiter if needed
)
```

Zero external HTTP dependencies — the SDK uses only `net/http` from the standard library.

---

## 3. Interface-Based Design

The SDK is built around a `Client` interface. This enables callers to mock the SDK in their own tests without needing a real server.

```go
// client.go
package OpenZep

import (
    "context"
    "time"
)

// Client defines the full OpenZep SDK surface.
// Implemented by *clientImpl. Use NewClient to construct.
type Client interface {
    // --- Memory ---
    AddMemory(ctx context.Context, userID string, req *AddMemoryRequest) (*AddMemoryResponse, error)
    GetContext(ctx context.Context, userID string, query string, opts ...ContextOption) (string, error)
    GetContextJSON(ctx context.Context, userID string, query string, opts ...ContextOption) (*ContextJSONResponse, error)
    DeleteMemory(ctx context.Context, userID string) error

    // --- Facts ---
    AddFacts(ctx context.Context, userID string, facts []FactInput) (*AddFactsResponse, error)
    ListFacts(ctx context.Context, userID string, opts ...ListOption) (*FactPage, error)
    ListAllFacts(ctx context.Context, userID string, opts ...ListOption) *FactIterator
    DeleteFact(ctx context.Context, userID string, factID string) error

    // --- Graph ---
    GraphNodes(ctx context.Context, userID string, typeFilter string) ([]GraphNode, error)
    GraphNode(ctx context.Context, userID string, nodeID string) (*GraphNode, error)
    GraphSearch(ctx context.Context, userID string, query string, types []string, limit int) ([]GraphSearchResult, error)
    GraphEdges(ctx context.Context, userID string, subject, predicate string) ([]GraphEdge, error)
    GraphCommunities(ctx context.Context, userID string) ([]CommunitySummary, error)

    // --- Users ---
    CreateUser(ctx context.Context, req *CreateUserRequest) (*User, error)
    GetUser(ctx context.Context, userID string) (*User, error)
    UpdateUser(ctx context.Context, userID string, req *UpdateUserRequest) (*User, error)
    ListUsers(ctx context.Context, opts ...ListOption) (*UserPage, error)
    ListAllUsers(ctx context.Context, opts ...ListOption) *UserIterator
    DeleteUser(ctx context.Context, userID string) error

    // --- Sessions ---
    CreateSession(ctx context.Context, userID string, req *CreateSessionRequest) (*Session, error)
    ListSessions(ctx context.Context, userID string, opts ...ListOption) (*SessionPage, error)
    ListAllSessions(ctx context.Context, userID string, opts ...ListOption) *SessionIterator
    GetSession(ctx context.Context, userID string, sessionID string) (*Session, error)
    SessionMessages(ctx context.Context, userID string, sessionID string, opts ...ListOption) (*MessagePage, error)
    DeleteSession(ctx context.Context, userID string, sessionID string) error

    // Close releases underlying resources (idle connections).
    Close() error
}
```

---

## 4. Constructor + Options

### 4.1 Configuration via functional options

```go
// options.go
package OpenZep

import (
    "net/http"
    "time"
)

type config struct {
    apiKey      string
    baseURL     string
    httpClient  *http.Client
    readTimeout time.Duration
    writeTimeout time.Duration
    maxRetries  int
    logger      Logger
}

// Option is a functional option for NewClient.
type Option func(*config)

// WithAPIKey sets the API key. Also read from MEMGRAPH_API_KEY env var.
func WithAPIKey(key string) Option {
    return func(c *config) { c.apiKey = key }
}

// WithBaseURL sets the OpenZep server URL (without /v1).
// Default: http://localhost:8000. Also read from MEMGRAPH_BASE_URL env var.
func WithBaseURL(url string) Option {
    return func(c *config) { c.baseURL = url }
}

// WithHTTPClient injects a custom *http.Client. Useful for testing with httptest.
func WithHTTPClient(client *http.Client) Option {
    return func(c *config) { c.httpClient = client }
}

// WithReadTimeout sets per-request read timeout. Default: 30s.
func WithReadTimeout(d time.Duration) Option {
    return func(c *config) { c.readTimeout = d }
}

// WithWriteTimeout sets per-request write timeout. Default: 60s.
func WithWriteTimeout(d time.Duration) Option {
    return func(c *config) { c.writeTimeout = d }
}

// WithRetryConfig sets retry parameters.
func WithRetryConfig(maxRetries int) Option {
    return func(c *config) { c.maxRetries = maxRetries }
}

// WithLogger sets a logger for debug output.
// If nil, no debug logging is performed.
func WithLogger(l Logger) Option {
    return func(c *config) { c.logger = l }
}

// Logger is the minimal interface for debug logging.
type Logger interface {
    Debugf(format string, args ...any)
}

// defaultConfig returns the default configuration.
func defaultConfig() *config {
    return &config{
        baseURL:     envOrDefault("MEMGRAPH_BASE_URL", "http://localhost:8000"),
        readTimeout: 30 * time.Second,
        writeTimeout: 60 * time.Second,
        maxRetries:  3,
        httpClient:  &http.Client{Timeout: 60 * time.Second},
    }
}

func envOrDefault(key, defaultVal string) string {
    if v := os.Getenv(key); v != "" {
        return v
    }
    return defaultVal
}
```

### 4.2 NewClient

```go
// client.go
package OpenZep

import (
    "fmt"
    "net/http"
    "os"
)

// NewClient creates a new OpenZep client.
// At minimum, an API key must be provided via WithAPIKey or MEMGRAPH_API_KEY env var.
func NewClient(opts ...Option) (Client, error) {
    cfg := defaultConfig()
    for _, opt := range opts {
        opt(cfg)
    }

    // Resolve API key
    if cfg.apiKey == "" {
        cfg.apiKey = os.Getenv("MEMGRAPH_API_KEY")
    }
    if cfg.apiKey == "" {
        return nil, fmt.Errorf("OpenZep: API key is required — pass WithAPIKey or set MEMGRAPH_API_KEY")
    }

    // Normalise base URL
    baseURL := cfg.baseURL
    if baseURL == "" {
        baseURL = "http://localhost:8000"
    }
    baseURL = strings.TrimRight(baseURL, "/") + "/v1"

    // Build transport
    transport := newTransport(&transportConfig{
        apiKey:      cfg.apiKey,
        baseURL:     baseURL,
        httpClient:  cfg.httpClient,
        readTimeout: cfg.readTimeout,
        writeTimeout: cfg.writeTimeout,
        maxRetries:  cfg.maxRetries,
        logger:      cfg.logger,
    })

    return &clientImpl{transport: transport}, nil
}

// clientImpl implements Client.
type clientImpl struct {
    transport *transport
}

// Close closes the underlying HTTP transport's idle connections.
func (c *clientImpl) Close() error {
    c.transport.httpClient.CloseIdleConnections()
    return nil
}
```

---

## 5. HTTP Transport

```go
// transport.go
package OpenZep

import (
    "bytes"
    "context"
    "encoding/json"
    "fmt"
    "io"
    "math"
    "math/rand"
    "net/http"
    "time"
)

type transportConfig struct {
    apiKey      string
    baseURL     string
    httpClient  *http.Client
    readTimeout time.Duration
    writeTimeout time.Duration
    maxRetries  int
    logger      Logger
}

type transport struct {
    config *transportConfig
    version string // set at init time
}

func newTransport(cfg *transportConfig) *transport {
    return &transport{
        config: cfg,
        version: Version,
    }
}

// request sends an HTTP request and unmarshals the JSON response into dest.
// dest must be a pointer (or nil for 204 No Content).
func (t *transport) request(ctx context.Context, method, path string, body any, dest any, opts *requestOptions) error {
    url := t.config.baseURL + path

    // Serialize body
    var reqBody io.Reader
    if body != nil {
        jsonBody, err := json.Marshal(body)
        if err != nil {
            return fmt.Errorf("OpenZep: marshal request body: %w", err)
        }
        reqBody = bytes.NewReader(jsonBody)
    }

    // Determine timeout
    timeout := t.config.readTimeout
    if method == http.MethodPost || method == http.MethodPatch || method == http.MethodDelete {
        timeout = t.config.writeTimeout
    }
    if opts != nil && opts.timeout > 0 {
        timeout = opts.timeout
    }

    var lastErr error

    for attempt := 0; attempt <= t.config.maxRetries; attempt++ {
        // Build request
        req, err := http.NewRequestWithContext(ctx, method, url, reqBody)
        if err != nil {
            return fmt.Errorf("OpenZep: create request: %w", err)
        }

        // Headers
        req.Header.Set("Authorization", "Bearer "+t.config.apiKey)
        req.Header.Set("User-Agent", fmt.Sprintf("OpenZep-go-sdk/%s", t.version))
        req.Header.Set("Content-Type", "application/json")

        // Apply timeout via context
        reqCtx, cancel := context.WithTimeout(req.Context(), timeout)
        req = req.WithContext(reqCtx)

        // Execute
        resp, err := t.config.httpClient.Do(req)
        cancel()

        if err != nil {
            lastErr = fmt.Errorf("OpenZep: request failed: %w", err)

            // Network errors are retryable
            if attempt < t.config.maxRetries {
                t.sleep(attempt)
                continue
            }
            // Check if context deadline exceeded → TimeoutError
            if ctx.Err() == context.DeadlineExceeded {
                return &TimeoutError{Message: fmt.Sprintf("request timed out after %s", timeout)}
            }
            return &ConnectionError{Message: fmt.Sprintf("connection failed: %s", err.Error())}
        }
        defer resp.Body.Close()

        // Debug log
        if t.config.logger != nil {
            t.config.logger.Debugf("OpenZep: %s %s -> %d (attempt %d/%d)",
                method, path, resp.StatusCode, attempt+1, t.config.maxRetries+1)
        }

        // Retry on 429 or 5xx
        if (resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode >= 500) && attempt < t.config.maxRetries {
            resp.Body.Close()
            retryAfter := parseRetryAfter(resp)
            wait := retryAfter
            if wait == 0 {
                wait = computeBackoff(attempt)
            }
            if t.config.logger != nil {
                t.config.logger.Debugf("OpenZep: retrying in %v (status=%d)", wait, resp.StatusCode)
            }
            t.sleepDuration(wait)
            continue
        }

        // Map error status to typed error
        if resp.StatusCode >= 400 {
            return mapHTTPError(resp)
        }

        // Read body if dest is provided
        if dest != nil {
            respBody, err := io.ReadAll(resp.Body)
            if err != nil {
                return fmt.Errorf("OpenZep: read response body: %w", err)
            }
            if len(respBody) == 0 {
                return nil // 204 No Content
            }
            if err := json.Unmarshal(respBody, dest); err != nil {
                return fmt.Errorf("OpenZep: unmarshal response: %w", err)
            }
        }

        return nil
    }

    return &MaxRetriesExceededError{
        Message:   fmt.Sprintf("request failed after %d retries", t.config.maxRetries),
        LastError: lastErr,
    }
}

// requestOptions allow per-call overrides.
type requestOptions struct {
    timeout time.Duration
}

func computeBackoff(attempt int) time.Duration {
    base := float64(time.Second)
    max := float64(30 * time.Second)
    delay := math.Min(base*math.Pow(2, float64(attempt)), max)
    jitter := rand.Float64() * float64(500*time.Millisecond)
    return time.Duration(delay + jitter)
}

func parseRetryAfter(resp *http.Response) time.Duration {
    header := resp.Header.Get("Retry-After")
    if header == "" {
        return 0
    }
    seconds, err := strconv.Atoi(header)
    if err != nil {
        return 0
    }
    return time.Duration(seconds) * time.Second
}

func (t *transport) sleep(attempt int) {
    t.sleepDuration(computeBackoff(attempt))
}

func (t *transport) sleepDuration(d time.Duration) {
    time.Sleep(d)
}
```

---

## 6. Error Handling — Sentinel Errors

```go
// errors.go
package OpenZep

import (
    "fmt"
    "net/http"
)

// Base error type. All OpenZep errors satisfy this interface.
type MemGraphError struct {
    Message   string
    StatusCode int
    RequestID string
}

func (e *MemGraphError) Error() string {
    if e.RequestID != "" {
        return fmt.Sprintf("OpenZep: %s (status=%d, request_id=%s)", e.Message, e.StatusCode, e.RequestID)
    }
    return fmt.Sprintf("OpenZep: %s (status=%d)", e.Message, e.StatusCode)
}

// Sentinel error types for errors.Is.
var (
    ErrAuthentication = &AuthenticationError{Message: "authentication failed"}
    ErrNotFound       = &NotFoundError{Message: "resource not found"}
    ErrRateLimit      = &RateLimitError{Message: "rate limit exceeded"}
    ErrValidation     = &ValidationError{Message: "request validation failed"}
    ErrServer         = &ServerError{Message: "internal server error"}
)

type AuthenticationError struct{ MemGraphError }
type NotFoundError struct{ MemGraphError }
type RateLimitError struct {
    MemGraphError
    RetryAfterSeconds int
}
type ValidationError struct{ MemGraphError }
type ServerError struct{ MemGraphError }
type ConnectionError struct {
    Message string
    Err     error
}
func (e *ConnectionError) Error() string { return fmt.Sprintf("OpenZep: connection error: %s", e.Message) }
func (e *ConnectionError) Unwrap() error { return e.Err }

type TimeoutError struct {
    Message string
    Err     error
}
func (e *TimeoutError) Error() string { return fmt.Sprintf("OpenZep: timeout: %s", e.Message) }
func (e *TimeoutError) Unwrap() error { return e.Err }

type MaxRetriesExceededError struct {
    Message   string
    LastError error
}
func (e *MaxRetriesExceededError) Error() string { return e.Message }
func (e *MaxRetriesExceededError) Unwrap() error { return e.LastError }

// mapHTTPError translates an HTTP response into a typed error.
func mapHTTPError(resp *http.Response) error {
    // Try to parse structured error body
    var errPayload struct {
        Error struct {
            Code       string `json:"code"`
            Message    string `json:"message"`
            RequestID  string `json:"request_id"`
        } `json:"error"`
    }
    body, _ := io.ReadAll(resp.Body)
    resp.Body.Close()
    json.Unmarshal(body, &errPayload) // best-effort parse

    msg := errPayload.Error.Message
    if msg == "" {
        msg = resp.Status
    }
    reqID := errPayload.Error.RequestID

    switch resp.StatusCode {
    case http.StatusUnauthorized:
        return &AuthenticationError{MemGraphError{msg, resp.StatusCode, reqID}}
    case http.StatusNotFound:
        return &NotFoundError{MemGraphError{msg, resp.StatusCode, reqID}}
    case http.StatusTooManyRequests:
        retryAfter := parseRetryAfter(resp)
        return &RateLimitError{
            MemGraphError:     MemGraphError{msg, resp.StatusCode, reqID},
            RetryAfterSeconds: int(retryAfter.Seconds()),
        }
    case http.StatusUnprocessableEntity:
        return &ValidationError{MemGraphError{msg, resp.StatusCode, reqID}}
    default:
        if resp.StatusCode >= 500 {
            return &ServerError{MemGraphError{msg, resp.StatusCode, reqID}}
        }
        return &MemGraphError{msg, resp.StatusCode, reqID}
    }
}
```

**Usage:**

```go
_, err := client.GetUser(ctx, "nonexistent")
if errors.Is(err, ErrNotFound) {
    fmt.Println("User not found — that's ok")
}

var rateLimitErr *RateLimitError
if errors.As(err, &rateLimitErr) {
    fmt.Printf("Rate limited, retry after %ds\n", rateLimitErr.RetryAfterSeconds)
}
```

---

## 7. Pagination

```go
// pagination.go
package OpenZep

import "encoding/json"

// Page is a generic paginated response.
type Page[T any] struct {
    Data       []T    `json:"data"`
    NextCursor string `json:"next_cursor,omitempty"`
    HasMore    bool   `json:"has_more"`
}

// PageResponse is the raw wire format for JSON decoding.
type PageResponse struct {
    Data       json.RawMessage `json:"data"`
    NextCursor string          `json:"next_cursor,omitempty"`
    HasMore    bool            `json:"has_more"`
}

// FactPage is a concrete page type for facts.
type FactPage = Page[Fact]

// UserPage is a concrete page type for users.
type UserPage = Page[User]

// SessionPage is a concrete page type for sessions.
type SessionPage = Page[Session]

// MessagePage is a concrete page type for messages.
type MessagePage = Page[Message]

// -- Iterator helpers --

// FactIterator iterates over all facts, auto-fetching pages.
type FactIterator struct {
    fetcher func(cursor string) (*FactPage, error)
    cursor  string
    items   []Fact
    index   int
    done    bool
}

func (it *FactIterator) Next() (*Fact, error) {
    for {
        if it.index < len(it.items) {
            item := &it.items[it.index]
            it.index++
            return item, nil
        }
        if it.done {
            return nil, nil // iterator exhausted
        }
        page, err := it.fetcher(it.cursor)
        if err != nil {
            return nil, err
        }
        it.items = page.Data
        it.index = 0
        if !page.HasMore || len(page.Data) == 0 {
            it.done = true
        }
        it.cursor = page.NextCursor
    }
}

// UserIterator and SessionIterator follow the same pattern.
```

---

## 8. Domain Implementations

### 8.1 Memory Domain

```go
// memory.go
package OpenZep

import (
    "context"
    "time"
)

// --- Types ---

type AddMemoryRequest struct {
    Messages  []MessageInput `json:"messages"`
    SessionID string         `json:"session_id,omitempty"`
}

type MessageInput struct {
    Role      string            `json:"role"` // user|assistant|system|tool
    Content   string            `json:"content"`
    CreatedAt *time.Time        `json:"created_at,omitempty"`
    Metadata  map[string]any    `json:"metadata,omitempty"`
}

type AddMemoryResponse struct {
    JobID  string `json:"job_id"`
    Status string `json:"status"`
}

type ContextOption func(*contextOptions)

type contextOptions struct {
    limit  int
    format string // "text" or "json"
}

func WithLimit(limit int) ContextOption {
    return func(o *contextOptions) { o.limit = limit }
}

func WithFormatJSON() ContextOption {
    return func(o *contextOptions) { o.format = "json" }
}

type ContextJSONResponse struct {
    Facts      []map[string]any `json:"facts"`
    Episodes   []map[string]any `json:"episodes"`
    Entities   []map[string]any `json:"entities"`
    Communities []map[string]any `json:"communities"`
}

// --- Implementation ---

func (c *clientImpl) AddMemory(ctx context.Context, userID string, req *AddMemoryRequest) (*AddMemoryResponse, error) {
    var resp AddMemoryResponse
    err := c.transport.request(ctx, http.MethodPost, "/users/"+userID+"/memory", req, &resp, nil)
    if err != nil {
        return nil, err
    }
    return &resp, nil
}

func (c *clientImpl) GetContext(ctx context.Context, userID string, query string, opts ...ContextOption) (string, error) {
    cfg := &contextOptions{limit: 10, format: "text"}
    for _, opt := range opts {
        opt(cfg)
    }

    // Build query params via map for the transport
    path := fmt.Sprintf("/users/%s/context?query=%s&limit=%d&format=%s",
        url.PathEscape(userID), url.QueryEscape(query), cfg.limit, cfg.format)

    if cfg.format == "json" {
        return "", fmt.Errorf("OpenZep: use GetContextJSON for format=json")
    }

    var raw string
    err := c.transport.request(ctx, http.MethodGet, path, nil, &raw, nil)
    if err != nil {
        return "", err
    }
    return raw, nil
}

func (c *clientImpl) GetContextJSON(ctx context.Context, userID string, query string, opts ...ContextOption) (*ContextJSONResponse, error) {
    opts = append(opts, WithFormatJSON())
    cfg := &contextOptions{limit: 10, format: "json"}
    for _, opt := range opts {
        opt(cfg)
    }

    path := fmt.Sprintf("/users/%s/context?query=%s&limit=%d&format=%s",
        url.PathEscape(userID), url.QueryEscape(query), cfg.limit, cfg.format)

    var resp ContextJSONResponse
    err := c.transport.request(ctx, http.MethodGet, path, nil, &resp, nil)
    if err != nil {
        return nil, err
    }
    return &resp, nil
}

func (c *clientImpl) DeleteMemory(ctx context.Context, userID string) error {
    return c.transport.request(ctx, http.MethodDelete, "/users/"+userID+"/memory", nil, nil, nil)
}
```

### 8.2 Facts Domain

```go
// facts.go
package OpenZep

import "context"

// --- Types ---

type FactInput struct {
    Subject   string `json:"subject"`
    Predicate string `json:"predicate"`
    Object    string `json:"object"`
    ValidAt   string `json:"valid_at,omitempty"`
    ExpiresAt string `json:"expires_at,omitempty"`
}

type AddFactsResponse struct {
    Inserted int `json:"inserted"`
}

type Fact struct {
    ID        string   `json:"id"`
    UserID    string   `json:"user_id"`
    Content   string   `json:"content"`
    Subject   string   `json:"subject,omitempty"`
    Predicate string   `json:"predicate,omitempty"`
    Object    string   `json:"object,omitempty"`
    Confidence float64 `json:"confidence"`
    CreatedAt string   `json:"created_at"`
}

// ListOption defines optional parameters for list operations.
type ListOption func(*listOptions)

type listOptions struct {
    limit  int
    cursor string
}

func WithLimit(limit int) ListOption {
    return func(o *listOptions) { o.limit = limit }
}

func WithCursor(cursor string) ListOption {
    return func(o *listOptions) { o.cursor = cursor }
}

// --- Implementation ---

func (c *clientImpl) AddFacts(ctx context.Context, userID string, facts []FactInput) (*AddFactsResponse, error) {
    body := map[string]any{"facts": facts}
    var resp AddFactsResponse
    err := c.transport.request(ctx, http.MethodPost, "/users/"+userID+"/facts", body, &resp, nil)
    if err != nil {
        return nil, err
    }
    return &resp, nil
}

func (c *clientImpl) ListFacts(ctx context.Context, userID string, opts ...ListOption) (*FactPage, error) {
    cfg := &listOptions{limit: 50}
    for _, opt := range opts {
        opt(cfg)
    }

    path := fmt.Sprintf("/users/%s/facts?limit=%d", url.PathEscape(userID), cfg.limit)
    if cfg.cursor != "" {
        path += "&cursor=" + url.QueryEscape(cfg.cursor)
    }

    var raw PageResponse
    var page FactPage
    err := c.transport.request(ctx, http.MethodGet, path, nil, &raw, nil)
    if err != nil {
        return nil, err
    }
    // Unmarshal inner data
    if err := json.Unmarshal(raw.Data, &page.Data); err != nil {
        return nil, fmt.Errorf("OpenZep: unmarshal fact page data: %w", err)
    }
    page.NextCursor = raw.NextCursor
    page.HasMore = raw.HasMore
    return &page, nil
}

func (c *clientImpl) ListAllFacts(ctx context.Context, userID string, opts ...ListOption) *FactIterator {
    cfg := &listOptions{limit: 50}
    for _, opt := range opts {
        opt(cfg)
    }

    return &FactIterator{
        fetcher: func(cursor string) (*FactPage, error) {
            return c.ListFacts(ctx, userID, append(opts, WithCursor(cursor))...)
        },
    }
}

func (c *clientImpl) DeleteFact(ctx context.Context, userID string, factID string) error {
    return c.transport.request(ctx, http.MethodDelete, fmt.Sprintf("/users/%s/facts/%s", url.PathEscape(userID), url.PathEscape(factID)), nil, nil, nil)
}
```

### 8.3 Graph Domain

```go
// graph.go
package OpenZep

import "context"

type GraphNode struct {
    UUID      string `json:"uuid"`
    Name      string `json:"name"`
    Type      string `json:"type"`
    Summary   string `json:"summary,omitempty"`
    CreatedAt string `json:"created_at"`
}

type GraphEdge struct {
    UUID      string `json:"uuid"`
    Subject   string `json:"subject"`
    Predicate string `json:"predicate"`
    Object    string `json:"object"`
    Fact      string `json:"fact,omitempty"`
    ValidAt   string `json:"valid_at,omitempty"`
    InvalidAt string `json:"invalid_at,omitempty"`
}

type GraphSearchResult struct {
    ID      string  `json:"id"`
    Type    string  `json:"type"`
    Content string  `json:"content"`
    Score   float64 `json:"score"`
}

type CommunitySummary struct {
    UUID    string `json:"uuid"`
    Name    string `json:"name"`
    Summary string `json:"summary"`
}

func (c *clientImpl) GraphNodes(ctx context.Context, userID string, typeFilter string) ([]GraphNode, error) {
    path := fmt.Sprintf("/users/%s/graph/nodes", url.PathEscape(userID))
    if typeFilter != "" {
        path += "?type=" + url.QueryEscape(typeFilter)
    }
    var raw struct {
        Data []GraphNode `json:"data"`
    }
    err := c.transport.request(ctx, http.MethodGet, path, nil, &raw, nil)
    if err != nil {
        return nil, err
    }
    return raw.Data, nil
}

func (c *clientImpl) GraphNode(ctx context.Context, userID string, nodeID string) (*GraphNode, error) {
    var node GraphNode
    err := c.transport.request(ctx, http.MethodGet,
        fmt.Sprintf("/users/%s/graph/nodes/%s", url.PathEscape(userID), url.PathEscape(nodeID)),
        nil, &node, nil)
    if err != nil {
        return nil, err
    }
    return &node, nil
}

func (c *clientImpl) GraphSearch(ctx context.Context, userID string, query string, types []string, limit int) ([]GraphSearchResult, error) {
    path := fmt.Sprintf("/users/%s/search?query=%s&limit=%d",
        url.PathEscape(userID), url.QueryEscape(query), limit)
    if len(types) > 0 {
        path += "&types=" + url.QueryEscape(strings.Join(types, ","))
    }
    var raw struct {
        Data []GraphSearchResult `json:"data"`
    }
    err := c.transport.request(ctx, http.MethodGet, path, nil, &raw, nil)
    if err != nil {
        return nil, err
    }
    return raw.Data, nil
}

func (c *clientImpl) GraphEdges(ctx context.Context, userID string, subject, predicate string) ([]GraphEdge, error) {
    path := fmt.Sprintf("/users/%s/graph/edges", url.PathEscape(userID))
    var params []string
    if subject != "" {
        params = append(params, "subject="+url.QueryEscape(subject))
    }
    if predicate != "" {
        params = append(params, "predicate="+url.QueryEscape(predicate))
    }
    if len(params) > 0 {
        path += "?" + strings.Join(params, "&")
    }
    var raw struct {
        Data []GraphEdge `json:"data"`
    }
    err := c.transport.request(ctx, http.MethodGet, path, nil, &raw, nil)
    if err != nil {
        return nil, err
    }
    return raw.Data, nil
}

func (c *clientImpl) GraphCommunities(ctx context.Context, userID string) ([]CommunitySummary, error) {
    var raw struct {
        Data []CommunitySummary `json:"data"`
    }
    err := c.transport.request(ctx, http.MethodGet,
        fmt.Sprintf("/users/%s/graph/communities", url.PathEscape(userID)),
        nil, &raw, nil)
    if err != nil {
        return nil, err
    }
    return raw.Data, nil
}
```

### 8.4 Users Domain

```go
// users.go
package OpenZep

import "context"

type CreateUserRequest struct {
    ExternalID string            `json:"external_id"`
    Name       string            `json:"name,omitempty"`
    Email      string            `json:"email,omitempty"`
    Metadata   map[string]any    `json:"metadata,omitempty"`
}

type UpdateUserRequest struct {
    Name     *string           `json:"name,omitempty"`
    Email    *string           `json:"email,omitempty"`
    Metadata *map[string]any   `json:"metadata,omitempty"`
}

type User struct {
    ID         string            `json:"id"`
    ExternalID string            `json:"external_id"`
    Name       string            `json:"name,omitempty"`
    Email      string            `json:"email,omitempty"`
    Metadata   map[string]any    `json:"metadata,omitempty"`
    CreatedAt  string            `json:"created_at"`
    UpdatedAt  string            `json:"updated_at"`
}

func (c *clientImpl) CreateUser(ctx context.Context, req *CreateUserRequest) (*User, error) {
    var user User
    err := c.transport.request(ctx, http.MethodPost, "/users", req, &user, nil)
    if err != nil {
        return nil, err
    }
    return &user, nil
}

func (c *clientImpl) GetUser(ctx context.Context, userID string) (*User, error) {
    var user User
    err := c.transport.request(ctx, http.MethodGet, "/users/"+url.PathEscape(userID), nil, &user, nil)
    if err != nil {
        return nil, err
    }
    return &user, nil
}

func (c *clientImpl) UpdateUser(ctx context.Context, userID string, req *UpdateUserRequest) (*User, error) {
    var user User
    err := c.transport.request(ctx, http.MethodPatch, "/users/"+url.PathEscape(userID), req, &user, nil)
    if err != nil {
        return nil, err
    }
    return &user, nil
}

func (c *clientImpl) ListUsers(ctx context.Context, opts ...ListOption) (*UserPage, error) {
    cfg := &listOptions{limit: 50}
    for _, opt := range opts {
        opt(cfg)
    }
    path := fmt.Sprintf("/users?limit=%d", cfg.limit)
    if cfg.cursor != "" {
        path += "&cursor=" + url.QueryEscape(cfg.cursor)
    }
    var raw PageResponse
    var page UserPage
    err := c.transport.request(ctx, http.MethodGet, path, nil, &raw, nil)
    if err != nil {
        return nil, err
    }
    if err := json.Unmarshal(raw.Data, &page.Data); err != nil {
        return nil, fmt.Errorf("OpenZep: unmarshal user page: %w", err)
    }
    page.NextCursor = raw.NextCursor
    page.HasMore = raw.HasMore
    return &page, nil
}

func (c *clientImpl) ListAllUsers(ctx context.Context, opts ...ListOption) *UserIterator {
    cfg := &listOptions{limit: 50}
    for _, opt := range opts {
        opt(cfg)
    }
    return &UserIterator{
        fetcher: func(cursor string) (*UserPage, error) {
            return c.ListUsers(ctx, append(opts, WithCursor(cursor))...)
        },
    }
}

func (c *clientImpl) DeleteUser(ctx context.Context, userID string) error {
    return c.transport.request(ctx, http.MethodDelete, "/users/"+url.PathEscape(userID), nil, nil, nil)
}
```

### 8.5 Sessions Domain

```go
// sessions.go
package OpenZep

import "context"

type CreateSessionRequest struct {
    ExternalID string            `json:"external_id,omitempty"`
    Metadata   map[string]any    `json:"metadata,omitempty"`
}

type Session struct {
    ID         string            `json:"id"`
    UserID     string            `json:"user_id"`
    ExternalID string            `json:"external_id,omitempty"`
    Metadata   map[string]any    `json:"metadata,omitempty"`
    CreatedAt  string            `json:"created_at"`
    ClosedAt   string            `json:"closed_at,omitempty"`
}

type Message struct {
    ID        string            `json:"id"`
    Role      string            `json:"role"`
    Content   string            `json:"content"`
    Metadata  map[string]any    `json:"metadata,omitempty"`
    CreatedAt string            `json:"created_at"`
}

func (c *clientImpl) CreateSession(ctx context.Context, userID string, req *CreateSessionRequest) (*Session, error) {
    var session Session
    err := c.transport.request(ctx, http.MethodPost, "/users/"+url.PathEscape(userID)+"/sessions", req, &session, nil)
    if err != nil {
        return nil, err
    }
    return &session, nil
}

func (c *clientImpl) ListSessions(ctx context.Context, userID string, opts ...ListOption) (*SessionPage, error) {
    cfg := &listOptions{limit: 50}
    for _, opt := range opts {
        opt(cfg)
    }
    path := fmt.Sprintf("/users/%s/sessions?limit=%d", url.PathEscape(userID), cfg.limit)
    if cfg.cursor != "" {
        path += "&cursor=" + url.QueryEscape(cfg.cursor)
    }
    var raw PageResponse
    var page SessionPage
    err := c.transport.request(ctx, http.MethodGet, path, nil, &raw, nil)
    if err != nil {
        return nil, err
    }
    if err := json.Unmarshal(raw.Data, &page.Data); err != nil {
        return nil, fmt.Errorf("OpenZep: unmarshal session page: %w", err)
    }
    page.NextCursor = raw.NextCursor
    page.HasMore = raw.HasMore
    return &page, nil
}

func (c *clientImpl) ListAllSessions(ctx context.Context, userID string, opts ...ListOption) *SessionIterator {
    cfg := &listOptions{limit: 50}
    for _, opt := range opts {
        opt(cfg)
    }
    return &SessionIterator{
        fetcher: func(cursor string) (*SessionPage, error) {
            return c.ListSessions(ctx, userID, append(opts, WithCursor(cursor))...)
        },
    }
}

func (c *clientImpl) GetSession(ctx context.Context, userID string, sessionID string) (*Session, error) {
    var session Session
    err := c.transport.request(ctx, http.MethodGet,
        fmt.Sprintf("/users/%s/sessions/%s", url.PathEscape(userID), url.PathEscape(sessionID)),
        nil, &session, nil)
    if err != nil {
        return nil, err
    }
    return &session, nil
}

func (c *clientImpl) SessionMessages(ctx context.Context, userID string, sessionID string, opts ...ListOption) (*MessagePage, error) {
    cfg := &listOptions{limit: 50}
    for _, opt := range opts {
        opt(cfg)
    }
    path := fmt.Sprintf("/users/%s/sessions/%s/messages?limit=%d",
        url.PathEscape(userID), url.PathEscape(sessionID), cfg.limit)
    if cfg.cursor != "" {
        path += "&cursor=" + url.QueryEscape(cfg.cursor)
    }
    var raw PageResponse
    var page MessagePage
    err := c.transport.request(ctx, http.MethodGet, path, nil, &raw, nil)
    if err != nil {
        return nil, err
    }
    if err := json.Unmarshal(raw.Data, &page.Data); err != nil {
        return nil, fmt.Errorf("OpenZep: unmarshal message page: %w", err)
    }
    page.NextCursor = raw.NextCursor
    page.HasMore = raw.HasMore
    return &page, nil
}

func (c *clientImpl) DeleteSession(ctx context.Context, userID string, sessionID string) error {
    return c.transport.request(ctx, http.MethodDelete,
        fmt.Sprintf("/users/%s/sessions/%s", url.PathEscape(userID), url.PathEscape(sessionID)),
        nil, nil, nil)
}
```

---

## 9. Custom HTTP Transport for Testing

The `WithHTTPClient` option allows injecting any `*http.Client`. This is the idiomatic Go way to enable mocking:

```go
// In production
client, _ := OpenZep.NewClient(
    OpenZep.WithAPIKey("mg_live_abc"),
    OpenZep.WithBaseURL("https://mg.example.com"),
)

// In tests
func TestGetUser(t *testing.T) {
    server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        assert.Equal(t, "Bearer test_key", r.Header.Get("Authorization"))
        assert.Contains(t, r.Header.Get("User-Agent"), "OpenZep-go-sdk/")

        json.NewEncoder(w).Encode(map[string]any{
            "id": "u1", "external_id": "ext1", "name": "Alice",
        })
    }))
    defer server.Close()

    client, _ := OpenZep.NewClient(
        OpenZep.WithAPIKey("test_key"),
        OpenZep.WithBaseURL(server.URL),
        OpenZep.WithHTTPClient(server.Client()),
    )

    user, err := client.GetUser(context.Background(), "ext1")
    assert.NoError(t, err)
    assert.Equal(t, "Alice", user.Name)
}
```

---

## 10. Version Constant

```go
// version.go
package OpenZep

// Version is the SDK version, injected at build time or set manually before release.
// Must match the git tag.
const Version = "1.0.0"
```

---

## 11. Complete Usage Example

```go
package main

import (
    "context"
    "fmt"
    "log"
    "os"

    "github.com/thelinkAI/OpenZep-go"
)

func main() {
    ctx := context.Background()

    client, err := OpenZep.NewClient(
        OpenZep.WithAPIKey("mg_live_abc123"),
        OpenZep.WithBaseURL("https://mg.example.com"),
        OpenZep.WithLogger(log.New(os.Stderr, "", log.LstdFlags)),
    )
    if err != nil {
        log.Fatalf("create client: %v", err)
    }
    defer client.Close()

    // 1. Create a user
    user, err := client.CreateUser(ctx, &OpenZep.CreateUserRequest{
        ExternalID: "usr_001",
        Name:       "Alice",
        Email:      "alice@example.com",
    })
    if err != nil {
        log.Fatalf("create user: %v", err)
    }
    fmt.Printf("User: %s\n", user.ID)

    // 2. Create a session
    session, err := client.CreateSession(ctx, user.ID, &OpenZep.CreateSessionRequest{
        ExternalID: "sess_001",
    })
    if err != nil {
        log.Fatalf("create session: %v", err)
    }

    // 3. Ingest memory
    ingestResp, err := client.AddMemory(ctx, user.ID, &OpenZep.AddMemoryRequest{
        Messages: []OpenZep.MessageInput{
            {Role: "user", Content: "I prefer Python over JavaScript."},
            {Role: "assistant", Content: "Noted! I'll keep that in mind."},
        },
        SessionID: session.ID,
    })
    if err != nil {
        log.Fatalf("add memory: %v", err)
    }
    fmt.Printf("Ingested: %s\n", ingestResp.JobID)

    // 4. Get context
    contextStr, err := client.GetContext(ctx, user.ID, "programming preferences")
    if err != nil {
        log.Fatalf("get context: %v", err)
    }
    fmt.Printf("Context: %s\n", contextStr)

    // 5. Search graph
    results, err := client.GraphSearch(ctx, user.ID, "preferences", []string{"facts", "entities"}, 10)
    if err != nil {
        log.Fatalf("search: %v", err)
    }
    for _, r := range results {
        fmt.Printf("  %s: %s (score=%.2f)\n", r.Type, r.Content, r.Score)
    }

    // 6. Add business facts
    _, err = client.AddFacts(ctx, user.ID, []OpenZep.FactInput{
        {Subject: user.ID, Predicate: "purchased", Object: "Pro plan"},
    })
    if err != nil {
        log.Fatalf("add facts: %v", err)
    }

    // 7. List facts with iterator
    iter := client.ListAllFacts(ctx, user.ID)
    for {
        fact, err := iter.Next()
        if err != nil {
            log.Fatalf("iterate facts: %v", err)
        }
        if fact == nil {
            break
        }
        fmt.Printf("Fact: %s\n", fact.Content)
    }

    // 8. List users with iterator
    userIter := client.ListAllUsers(ctx)
    for {
        u, err := userIter.Next()
        if err != nil {
            log.Fatalf("iterate users: %v", err)
        }
        if u == nil {
            break
        }
        fmt.Printf("User: %s\n", u.Name)
    }
}
```

---

## 12. Testing

```go
// client_test.go
package OpenZep

import (
    "context"
    "encoding/json"
    "net/http"
    "net/http/httptest"
    "testing"

    "github.com/stretchr/testify/assert"
    "github.com/stretchr/testify/require"
)

func TestClient_AddMemory_Success(t *testing.T) {
    server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        assert.Equal(t, "POST", r.Method)
        assert.Equal(t, "/v1/users/u1/memory", r.URL.Path)
        assert.Equal(t, "Bearer test_key", r.Header.Get("Authorization"))
        assert.Contains(t, r.Header.Get("User-Agent"), "OpenZep-go-sdk/")

        w.WriteHeader(http.StatusAccepted)
        json.NewEncoder(w).Encode(map[string]string{
            "job_id": "j_abc",
            "status": "accepted",
        })
    }))
    defer server.Close()

    client, err := NewClient(
        WithAPIKey("test_key"),
        WithBaseURL(server.URL),
        WithHTTPClient(server.Client()),
    )
    require.NoError(t, err)

    resp, err := client.AddMemory(context.Background(), "u1", &AddMemoryRequest{
        Messages: []MessageInput{
            {Role: "user", Content: "Hello"},
        },
        SessionID: "s1",
    })
    require.NoError(t, err)
    assert.Equal(t, "j_abc", resp.JobID)
}

func TestClient_AuthenticationError(t *testing.T) {
    server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        w.WriteHeader(http.StatusUnauthorized)
        json.NewEncoder(w).Encode(map[string]any{
            "error": map[string]string{
                "code":    "UNAUTHORIZED",
                "message": "Invalid API key",
            },
        })
    }))
    defer server.Close()

    client, _ := NewClient(
        WithAPIKey("bad_key"),
        WithBaseURL(server.URL),
        WithHTTPClient(server.Client()),
    )

    _, err := client.GetUser(context.Background(), "u1")
    require.Error(t, err)
    assert.True(t, errors.Is(err, ErrAuthentication))
}

func TestClient_RetryOn429(t *testing.T) {
    attempt := 0
    server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        attempt++
        if attempt <= 2 {
            w.WriteHeader(http.StatusTooManyRequests)
            return
        }
        w.WriteHeader(http.StatusOK)
        json.NewEncoder(w).Encode(map[string]any{
            "id": "u1", "external_id": "ext1", "name": "Alice",
        })
    }))
    defer server.Close()

    client, _ := NewClient(
        WithAPIKey("test_key"),
        WithBaseURL(server.URL),
        WithHTTPClient(server.Client()),
        WithRetryConfig(3),
    )

    user, err := client.GetUser(context.Background(), "ext1")
    require.NoError(t, err)
    assert.Equal(t, "Alice", user.Name)
    assert.Equal(t, 3, attempt) // 2 retries + 1 success
}

func TestClient_NoRetryOn400(t *testing.T) {
    attempts := 0
    server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        attempts++
        w.WriteHeader(http.StatusBadRequest)
    }))
    defer server.Close()

    client, _ := NewClient(
        WithAPIKey("test_key"),
        WithBaseURL(server.URL),
        WithHTTPClient(server.Client()),
    )

    _, err := client.GetUser(context.Background(), "u1")
    require.Error(t, err)
    assert.Equal(t, 1, attempts) // no retry
}
```

```bash
go test ./... -v -race -cover
```

---

## 13. pkg.go.dev CI Publishing

```yaml
# .gitlab-ci.yml (in OpenZep-go repo)
publish-go:
  stage: release
  image: golang:1.22
  only:
    - tags
  script:
    - go test ./... -v -race -cover
    - go build ./...
    # pkg.go.dev auto-indexes from public repos on tags.
    # Trigger re-index explicitly:
    - curl -X POST https://proxy.golang.org/github.com/thelinkAI/OpenZep-go/@v/${CI_COMMIT_TAG}.info

---

## Implementation Status

**Status:** 🟡 Planned for v1.1. Not yet implemented.
```
