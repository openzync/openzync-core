# Python SDK Implementation Guide — `OpenZep-py`

> **Phase**: Phase 2 — Full Feature Parity (Week 5-7)
> **Priority**: P0
> **Package**: `OpenZep-py` on [PyPI](https://pypi.org/project/OpenZep-py/)
> **Source**: SRS §5.8.1

---

## 1. Package Overview

```
OpenZep-py/
├── src/OpenZep/
│   ├── __init__.py
│   ├── client.py          # OpenZep client + AsyncMemGraph
│   ├── models/
│   │   ├── memory.py      # MemoryIngestRequest, ContextResponse
│   │   ├── fact.py        # FactRequest, FactResponse
│   │   ├── graph.py       # GraphNode, GraphEdge, GraphSearchResult
│   │   ├── user.py        # UserCreateRequest, UserResponse
│   │   └── session.py     # SessionCreateRequest, SessionResponse
│   ├── _http.py           # HTTP transport, retry, auth, logging
│   ├── _errors.py         # Exception hierarchy
│   └── _pagination.py     # PageIterator, Page
├── tests/
│   ├── test_client.py
│   ├── test_http.py
│   ├── test_pagination.py
│   └── conftest.py
├── pyproject.toml          # Poetry / Flit config
├── README.md
└── LICENSE                 # Apache 2.0
```

---

## 2. Installation

```bash
pip install OpenZep-py
# or
poetry add OpenZep-py
```

Python 3.10+ required.

---

## 3. Sync / Async Duality

The SDK is **async-first** (built on `httpx.AsyncClient`) with a **sync wrapper** that runs the async implementation in an event loop.

### 3.1 Async client — primary implementation

```python
# src/OpenZep/_http.py
import httpx
from typing import Optional
from OpenZep._errors import _raise_on_error

class _AsyncHTTPTransport:
    """Low-level async HTTP transport with retry, auth, and logging."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        timeout_read: float = 30.0,
        timeout_write: float = 60.0,
        max_retries: int = 3,
        debug: bool = False,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/") + "/v1"
        self._max_retries = max_retries
        self._debug = debug

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_read, write=timeout_write),
            follow_redirects=True,
        )

    async def request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send a request with auth, retry, and logging."""
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._api_key}"
        headers["User-Agent"] = f"OpenZep-py-sdk/{__version__}"
        headers["Content-Type"] = "application/json"

        last_error: Optional[Exception] = None

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.request(method, path, headers=headers, **kwargs)
            except httpx.TimeoutException as e:
                last_error = TimeoutError(f"Request timed out after {self._client.timeout.read}s")
                continue  # retry on timeout
            except httpx.ConnectError as e:
                raise ConnectionError(f"Failed to connect to {self._base_url}") from e

            # Log if debug mode
            if self._debug:
                import logging
                logging.getLogger("OpenZep").debug(
                    f"{method} {path} -> {response.status_code} (attempt {attempt + 1})"
                )

            # Retry on 429 or 5xx
            if response.status_code in (429,) or response.status_code >= 500:
                if attempt < self._max_retries:
                    wait = _compute_backoff(attempt, response)
                    if self._debug:
                        logging.getLogger("OpenZep").debug(
                            f"Retrying in {wait:.2f}s (status={response.status_code})"
                        )
                    import asyncio
                    await asyncio.sleep(wait)
                    continue

            # Non-retryable or final attempt
            _raise_on_error(response)
            return response

        raise MaxRetriesExceededError(
            f"Request failed after {self._max_retries} retries",
            last_error=last_error,
        )

    async def close(self) -> None:
        await self._client.aclose()
```

### 3.2 Sync wrapper

```python
# src/OpenZep/client.py
import asyncio
from functools import wraps

def _syncify(async_method):
    """Decorator that runs an async method in a sync context using asyncio.run()."""

    @wraps(async_method)
    def wrapper(self, *args, **kwargs):
        return asyncio.run(async_method(self._async, *args, **kwargs))

    return wrapper


class OpenZep:
    """Synchronous OpenZep client.

    Every method wraps the async client with asyncio.run().
    Prefer AsyncMemGraph in async contexts.
    """

    def __init__(self, **kwargs: Any) -> None:
        self._async = AsyncMemGraph(**kwargs)

    @property
    def memory(self) -> MemoryDomain:
        return MemoryDomain(self._async.memory)

    @property
    def facts(self) -> FactsDomain:
        return FactsDomain(self._async.facts)

    @property
    def graph(self) -> GraphDomain:
        return GraphDomain(self._async.graph)

    @property
    def users(self) -> UsersDomain:
        return UsersDomain(self._async.users)

    @property
    def sessions(self) -> SessionsDomain:
        return SessionsDomain(self._async.sessions)

    # Re-export async methods as sync
    def __getattr__(self, name):
        return getattr(self._async, name) if not name.startswith("_") else super().__getattr__(name)
```

**Usage:**

```python
from OpenZep import OpenZep, AsyncMemGraph

# Sync (wraps asyncio.run internally)
client = OpenZep(api_key="mg_live_...")
response = client.memory.ingest(user_id="u1", session_id="s1", messages=[...])

# Async
async_client = AsyncMemGraph(api_key="mg_live_...")
response = await async_client.memory.ingest(user_id="u1", session_id="s1", messages=[...])
```

### 3.3 Important caveat

`asyncio.run()` cannot be called from within a running event loop. If the caller is already in an async context, they MUST use `AsyncMemGraph` directly. The sync wrapper is for scripts, REPL sessions, and synchronous orchestrators only.

---

## 4. Memory Domain

### 4.1 Ingestion

```python
# src/OpenZep/models/memory.py
from pydantic import BaseModel, ConfigDict
from datetime import datetime
from typing import Optional

class Message(BaseModel):
    role: str  # "user" | "assistant" | "system" | "tool"
    content: str
    created_at: Optional[datetime] = None
    metadata: Optional[dict] = None

class MemoryIngestResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")  # ← forward compat

    job_id: str
    status: str  # "accepted"

class ContextResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    context: str

class ContextJSONResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    facts: list[dict]
    episodes: list[dict]
    entities: list[dict]
    communities: list[dict]
```

```python
# src/OpenZep/domains/memory.py
from typing import Optional
from OpenZep._http import _AsyncHTTPTransport
from OpenZep.models.memory import Message, MemoryIngestResponse, ContextResponse

class MemoryDomain:
    """Access via client.memory"""

    def __init__(self, http: _AsyncHTTPTransport) -> None:
        self._http = http

    async def ingest(
        self,
        user_id: str,
        messages: list[Message | dict],
        session_id: Optional[str] = None,
        timeout_read: Optional[float] = None,
    ) -> MemoryIngestResponse:
        """Ingest messages into a user's memory.

        Returns 202 Accepted with a job_id for async enrichment.
        """
        payload: dict[str, Any] = {
            "messages": [
                m.model_dump() if isinstance(m, Message) else m
                for m in messages
            ],
        }
        if session_id:
            payload["session_id"] = session_id

        response = await self._http.request(
            "POST",
            f"/users/{user_id}/memory",
            json=payload,
            timeout=timeout_read,
        )
        return MemoryIngestResponse(**response.json())

    async def get_context(
        self,
        user_id: str,
        query: str,
        limit: int = 10,
        format: str = "text",  # "text" | "json"
        timeout_read: Optional[float] = None,
    ) -> str | ContextJSONResponse:
        """Get an assembled context block for LLM injection.

        Returns a plain string by default, or a structured JSON response.
        """
        params: dict[str, Any] = {"query": query, "limit": limit, "format": format}
        response = await self._http.request(
            "GET",
            f"/users/{user_id}/context",
            params=params,
            timeout=timeout_read,
        )
        if format == "json":
            return ContextJSONResponse(**response.json())
        return response.text  # plain string context

    async def delete(
        self,
        user_id: str,
        timeout_read: Optional[float] = None,
    ) -> None:
        """Wipe all memory (episodes, facts, graph nodes) for a user."""
        await self._http.request("DELETE", f"/users/{user_id}/memory")
```

---

## 5. Facts Domain

```python
# src/OpenZep/domains/facts.py
from typing import Optional
from OpenZep._pagination import PageIterator, Page
from OpenZep.models.fact import FactResponse

class FactCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    subject: str
    predicate: str
    object: str
    valid_at: Optional[str] = None   # ISO-8601
    expires_at: Optional[str] = None # ISO-8601

class FactsDomain:
    """Access via client.facts"""

    def __init__(self, http: _AsyncHTTPTransport) -> None:
        self._http = http

    async def add(
        self,
        user_id: str,
        facts: list[FactCreate | dict],
        timeout_write: Optional[float] = None,
    ) -> dict:
        """Add fact triples manually (business data ingestion).

        Accepts up to 500 facts per request.
        """
        payload = {
            "facts": [
                f.model_dump() if isinstance(f, FactCreate) else f
                for f in facts
            ],
        }
        response = await self._http.request(
            "POST",
            f"/users/{user_id}/facts",
            json=payload,
            timeout=timeout_write,
        )
        return response.json()

    async def list(
        self,
        user_id: str,
        limit: int = 50,
        cursor: Optional[str] = None,
        timeout_read: Optional[float] = None,
    ) -> Page[FactResponse]:
        """List facts for a user with cursor-based pagination."""
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        response = await self._http.request(
            "GET",
            f"/users/{user_id}/facts",
            params=params,
            timeout=timeout_read,
        )
        return Page[FactResponse].from_response(response.json(), FactResponse)

    def all(
        self,
        user_id: str,
        limit: int = 50,
    ) -> PageIterator[FactResponse]:
        """Iterate over all facts for a user, auto-fetching pages."""
        return PageIterator(
            fetcher=lambda cursor: self.list(user_id=user_id, limit=limit, cursor=cursor),
        )

    async def delete(
        self,
        user_id: str,
        fact_id: str,
    ) -> None:
        """Delete a single fact by ID."""
        await self._http.request("DELETE", f"/users/{user_id}/facts/{fact_id}")
```

---

## 6. Graph Domain

```python
# src/OpenZep/domains/graph.py
from typing import Optional
from OpenZep.models.graph import GraphNode, GraphEdge, GraphSearchResult

class GraphDomain:
    """Access via client.graph"""

    def __init__(self, http: _AsyncHTTPTransport) -> None:
        self._http = http

    async def nodes(
        self,
        user_id: str,
        type_filter: Optional[str] = None,  # e.g. "Person", "Company"
        timeout_read: Optional[float] = None,
    ) -> list[GraphNode]:
        """List all entity nodes for a user."""
        params: dict[str, Any] = {}
        if type_filter:
            params["type"] = type_filter
        response = await self._http.request(
            "GET",
            f"/users/{user_id}/graph/nodes",
            params=params,
            timeout=timeout_read,
        )
        return [GraphNode(**n) for n in response.json()["data"]]

    async def get_node(
        self,
        user_id: str,
        node_id: str,
        timeout_read: Optional[float] = None,
    ) -> GraphNode:
        """Get a single entity node with all edges."""
        response = await self._http.request(
            "GET",
            f"/users/{user_id}/graph/nodes/{node_id}",
            timeout=timeout_read,
        )
        return GraphNode(**response.json())

    async def search(
        self,
        user_id: str,
        query: str,
        types: Optional[list[str]] = None,  # ["facts", "episodes", "entities"]
        limit: int = 10,
        timeout_read: Optional[float] = None,
    ) -> list[GraphSearchResult]:
        """Hybrid search across the user's knowledge graph."""
        params: dict[str, Any] = {"query": query, "limit": limit}
        if types:
            params["types"] = ",".join(types)
        response = await self._http.request(
            "GET",
            f"/users/{user_id}/search",
            params=params,
            timeout=timeout_read,
        )
        return [GraphSearchResult(**r) for r in response.json()["data"]]

    async def edges(
        self,
        user_id: str,
        subject: Optional[str] = None,
        predicate: Optional[str] = None,
        timeout_read: Optional[float] = None,
    ) -> list[GraphEdge]:
        """List relationships with optional filters."""
        params: dict[str, Any] = {}
        if subject:
            params["subject"] = subject
        if predicate:
            params["predicate"] = predicate
        response = await self._http.request(
            "GET",
            f"/users/{user_id}/graph/edges",
            params=params,
            timeout=timeout_read,
        )
        return [GraphEdge(**e) for e in response.json()["data"]]

    async def communities(
        self,
        user_id: str,
        timeout_read: Optional[float] = None,
    ) -> list[dict]:
        """List community summary nodes."""
        response = await self._http.request(
            "GET",
            f"/users/{user_id}/graph/communities",
            timeout=timeout_read,
        )
        return response.json()["data"]
```

---

## 7. Users Domain

```python
# src/OpenZep/domains/users.py
from typing import Optional
from OpenZep._pagination import PageIterator, Page
from OpenZep.models.user import UserResponse

class UserCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    external_id: str
    name: Optional[str] = None
    email: Optional[str] = None
    metadata: Optional[dict] = None

class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: Optional[str] = None
    email: Optional[str] = None
    metadata: Optional[dict] = None

class UsersDomain:
    """Access via client.users"""

    def __init__(self, http: _AsyncHTTPTransport) -> None:
        self._http = http

    async def create(
        self,
        external_id: str,
        name: Optional[str] = None,
        email: Optional[str] = None,
        metadata: Optional[dict] = None,
        timeout_write: Optional[float] = None,
    ) -> UserResponse:
        """Create a new user."""
        payload = UserCreate(
            external_id=external_id,
            name=name,
            email=email,
            metadata=metadata or {},
        )
        response = await self._http.request(
            "POST",
            "/users",
            json=payload.model_dump(exclude_none=True),
            timeout=timeout_write,
        )
        return UserResponse(**response.json())

    async def get(
        self,
        user_id: str,
        timeout_read: Optional[float] = None,
    ) -> UserResponse:
        """Get a user by ID."""
        response = await self._http.request(
            "GET",
            f"/users/{user_id}",
            timeout=timeout_read,
        )
        return UserResponse(**response.json())

    async def update(
        self,
        user_id: str,
        name: Optional[str] = None,
        email: Optional[str] = None,
        metadata: Optional[dict] = None,
        timeout_write: Optional[float] = None,
    ) -> UserResponse:
        """Update user metadata."""
        payload = UserUpdate(name=name, email=email, metadata=metadata)
        response = await self._http.request(
            "PATCH",
            f"/users/{user_id}",
            json=payload.model_dump(exclude_none=True),
            timeout=timeout_write,
        )
        return UserResponse(**response.json())

    async def list(
        self,
        limit: int = 50,
        cursor: Optional[str] = None,
        timeout_read: Optional[float] = None,
    ) -> Page[UserResponse]:
        """List users with pagination."""
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        response = await self._http.request(
            "GET",
            "/users",
            params=params,
            timeout=timeout_read,
        )
        return Page[UserResponse].from_response(response.json(), UserResponse)

    def all(
        self,
        limit: int = 50,
    ) -> PageIterator[UserResponse]:
        """Iterate over all users."""
        return PageIterator(
            fetcher=lambda cursor: self.list(limit=limit, cursor=cursor),
        )

    async def delete(
        self,
        user_id: str,
        timeout_write: Optional[float] = None,
    ) -> None:
        """Delete a user and all associated data (GDPR-compliant cascade)."""
        await self._http.request("DELETE", f"/users/{user_id}")
```

---

## 8. Sessions Domain

```python
# src/OpenZep/domains/sessions.py
from typing import Optional
from OpenZep._pagination import PageIterator, Page
from OpenZep.models.session import SessionResponse, MessageResponse

class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    external_id: Optional[str] = None
    metadata: Optional[dict] = None

class SessionsDomain:
    """Access via client.sessions"""

    def __init__(self, http: _AsyncHTTPTransport) -> None:
        self._http = http

    async def create(
        self,
        user_id: str,
        external_id: Optional[str] = None,
        metadata: Optional[dict] = None,
        timeout_write: Optional[float] = None,
    ) -> SessionResponse:
        """Create a new session."""
        payload = SessionCreate(external_id=external_id, metadata=metadata)
        response = await self._http.request(
            "POST",
            f"/users/{user_id}/sessions",
            json=payload.model_dump(exclude_none=True),
            timeout=timeout_write,
        )
        return SessionResponse(**response.json())

    async def list(
        self,
        user_id: str,
        limit: int = 50,
        cursor: Optional[str] = None,
        timeout_read: Optional[float] = None,
    ) -> Page[SessionResponse]:
        """List sessions for a user."""
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        response = await self._http.request(
            "GET",
            f"/users/{user_id}/sessions",
            params=params,
            timeout=timeout_read,
        )
        return Page[SessionResponse].from_response(response.json(), SessionResponse)

    def all(
        self,
        user_id: str,
        limit: int = 50,
    ) -> PageIterator[SessionResponse]:
        """Iterate over all sessions for a user."""
        return PageIterator(
            fetcher=lambda cursor: self.list(user_id=user_id, limit=limit, cursor=cursor),
        )

    async def get(
        self,
        user_id: str,
        session_id: str,
        timeout_read: Optional[float] = None,
    ) -> SessionResponse:
        """Get a session's detail."""
        response = await self._http.request(
            "GET",
            f"/users/{user_id}/sessions/{session_id}",
            timeout=timeout_read,
        )
        return SessionResponse(**response.json())

    async def get_messages(
        self,
        user_id: str,
        session_id: str,
        limit: int = 50,
        cursor: Optional[str] = None,
        timeout_read: Optional[float] = None,
    ) -> Page[MessageResponse]:
        """Get paginated message history for a session."""
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        response = await self._http.request(
            "GET",
            f"/users/{user_id}/sessions/{session_id}/messages",
            params=params,
            timeout=timeout_read,
        )
        return Page[MessageResponse].from_response(response.json(), MessageResponse)

    async def delete(
        self,
        user_id: str,
        session_id: str,
        timeout_write: Optional[float] = None,
    ) -> None:
        """Delete a session and unlink from graph."""
        await self._http.request(
            "DELETE",
            f"/users/{user_id}/sessions/{session_id}",
        )
```

---

## 9. Error Classes

```python
# src/OpenZep/_errors.py
from typing import Optional

class MemGraphError(Exception):
    """Base exception for all OpenZep SDK errors."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        request_id: Optional[str] = None,
        response: Optional[Any] = None,
    ) -> None:
        self.message = message
        self.status_code = status_code
        self.request_id = request_id
        self.response = response
        super().__init__(self.message)


class AuthenticationError(MemGraphError):
    """401 — API key missing, invalid, or revoked."""


class NotFoundError(MemGraphError):
    """404 — Resource not found."""


class RateLimitError(MemGraphError):
    """429 — Rate limit exceeded."""

    def __init__(self, *args: Any, retry_after: Optional[int] = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.retry_after_seconds = retry_after


class ValidationError(MemGraphError):
    """422 — Request validation failed."""


class ServerError(MemGraphError):
    """5xx — Internal server error."""


class ConnectionError(MemGraphError):
    """Failed to connect to the server."""


class TimeoutError(MemGraphError):
    """Request exceeded the configured timeout."""


class MaxRetriesExceededError(MemGraphError):
    """Request failed after exhausting retry attempts."""


def _raise_on_error(response: httpx.Response) -> None:
    """Map HTTP status to typed exception."""
    body = _try_parse_json(response)
    request_id = (body or {}).get("error", {}).get("request_id") if body else None

    if response.status_code == 401:
        raise AuthenticationError(
            message=body.get("error", {}).get("message", "Authentication failed") if body else "Authentication failed",
            status_code=401,
            request_id=request_id,
            response=response,
        )
    if response.status_code == 404:
        raise NotFoundError(...)
    if response.status_code == 429:
        retry_after = _parse_retry_after(response)
        raise RateLimitError(..., retry_after=retry_after)
    if response.status_code == 422:
        raise ValidationError(...)
    if response.status_code >= 500:
        raise ServerError(...)
```

---

## 10. Pagination Helper

```python
# src/OpenZep/_pagination.py
from __future__ import annotations
from typing import Generic, TypeVar, Callable, Optional, AsyncIterator
from pydantic import BaseModel

T = TypeVar("T")

class Page(BaseModel, Generic[T]):
    data: list[T]
    next_cursor: Optional[str] = None
    has_more: bool = False

    @classmethod
    def from_response(cls, body: dict, item_cls: type[T]) -> Page[T]:
        return cls(
            data=[item_cls(**item) for item in body["data"]],
            next_cursor=body.get("next_cursor"),
            has_more=body.get("has_more", False),
        )


class PageIterator(AsyncIterator, Generic[T]):
    """Async iterator that auto-fetches next pages."""

    def __init__(
        self,
        fetcher: Callable[[Optional[str]], Awaitable[Page[T]]],
    ) -> None:
        self._fetcher = fetcher
        self._cursor: Optional[str] = None
        self._items: list[T] = []
        self._index: int = 0
        self._has_more: bool = True
        self._exhausted: bool = False

    def __aiter__(self) -> PageIterator[T]:
        return self

    async def __anext__(self) -> T:
        # Return from current page if available
        if self._index < len(self._items):
            item = self._items[self._index]
            self._index += 1
            return item

        if self._exhausted:
            raise StopAsyncIteration

        # Fetch next page
        page = await self._fetcher(self._cursor)
        self._items = page.data
        self._index = 0

        if not page.has_more or not page.data:
            self._exhausted = True

        self._cursor = page.next_cursor

        if not self._items:
            raise StopAsyncIteration

        # Return first item of new page
        self._index = 1
        return self._items[0]
```

---

## 11. Constructor + Configuration

```python
# src/OpenZep/client.py
import os
from typing import Optional
from OpenZep._http import _AsyncHTTPTransport

__version__ = "1.0.0"

class AsyncMemGraph:
    """Async OpenZep client. Use in async contexts."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout_read: float = 30.0,
        timeout_write: float = 60.0,
        max_retries: int = 3,
        debug: bool = False,
    ) -> None:
        resolved_key = api_key or os.environ.get("MEMGRAPH_API_KEY")
        resolved_url = base_url or os.environ.get("MEMGRAPH_BASE_URL", "http://localhost:8000")

        if not resolved_key:
            raise ValueError(
                "API key is required. Pass api_key= or set MEMGRAPH_API_KEY environment variable."
            )

        self._http = _AsyncHTTPTransport(
            api_key=resolved_key,
            base_url=resolved_url,
            timeout_read=timeout_read,
            timeout_write=timeout_write,
            max_retries=max_retries,
            debug=debug or os.environ.get("MEMGRAPH_DEBUG", "").lower() in ("1", "true"),
        )

        # Domain objects
        self.memory = MemoryDomain(self._http)
        self.facts = FactsDomain(self._http)
        self.graph = GraphDomain(self._http)
        self.users = UsersDomain(self._http)
        self.sessions = SessionsDomain(self._http)

    async def close(self) -> None:
        await self._http.close()
```

---

## 12. Public API — `__init__.py`

```python
# src/OpenZep/__init__.py
from OpenZep.client import OpenZep, AsyncMemGraph
from OpenZep._errors import (
    MemGraphError,
    AuthenticationError,
    NotFoundError,
    RateLimitError,
    ValidationError,
    ServerError,
    ConnectionError,
    TimeoutError,
    MaxRetriesExceededError,
)
from OpenZep.models.memory import Message, MemoryIngestResponse, ContextResponse
from OpenZep.models.fact import FactResponse
from OpenZep.models.graph import GraphNode, GraphEdge, GraphSearchResult
from OpenZep.models.user import UserResponse
from OpenZep.models.session import SessionResponse, MessageResponse

__all__ = [
    "OpenZep",
    "AsyncMemGraph",
    "MemGraphError",
    "AuthenticationError",
    "NotFoundError",
    "RateLimitError",
    "ValidationError",
    "ServerError",
    "ConnectionError",
    "TimeoutError",
    "MaxRetriesExceededError",
    "Message",
    "MemoryIngestResponse",
    "ContextResponse",
    "FactResponse",
    "GraphNode",
    "GraphEdge",
    "GraphSearchResult",
    "UserResponse",
    "SessionResponse",
    "MessageResponse",
]
```

---

## 13. pyproject.toml (Poetry)

```toml
[tool.poetry]
name = "OpenZep-py"
version = "1.0.0"
description = "OpenZep — open-source agent memory platform Python SDK"
authors = ["TheLinkAI <engineering@thelinkai.com>"]
license = "Apache-2.0"
readme = "README.md"
homepage = "https://github.com/thelinkai/OpenZep"
repository = "https://github.com/thelinkai/OpenZep-py"
keywords = ["OpenZep", "agent-memory", "knowledge-graph", "llm", "ai"]

[tool.poetry.dependencies]
python = "^3.10"
httpx = "^0.27"
pydantic = "^2.0"

[tool.poetry.group.dev.dependencies]
pytest = "^8.0"
pytest-asyncio = "^0.23"
respx = "^0.21"
ruff = "^0.3"

[build-system]
requires = ["poetry-core"]
build-backend = "poetry.core.masonry.api"

[tool.ruff]
line-length = 100
target-version = "py310"
```

---

## 14. PyPI Publishing CI

```yaml
# .gitlab-ci.yml (in OpenZep-py repo)
publish-pypi:
  stage: release
  image: python:3.12-slim
  only:
    - tags
  script:
    - pip install poetry
    - poetry build
    - poetry publish --username __token__ --password ${PYPI_TOKEN}
```

---

## 15. Complete Usage Example

```python
import asyncio
from OpenZep import AsyncMemGraph, Message

async def main():
    client = AsyncMemGraph(
        api_key="mg_live_abc123",
        base_url="https://mg.example.com",
    )

    # 1. Create a user
    user = await client.users.create(
        external_id="usr_001",
        name="Alice",
        email="alice@example.com",
    )
    print(f"Created user: {user.id}")

    # 2. Create a session
    session = await client.sessions.create(
        user_id=user.id,
        external_id="sess_001",
    )

    # 3. Ingest memory
    result = await client.memory.ingest(
        user_id=user.id,
        session_id=session.id,
        messages=[
            Message(role="user", content="I prefer Python over JavaScript."),
            Message(role="assistant", content="Noted! I'll keep that in mind."),
            Message(role="user", content="My favourite framework is FastAPI."),
        ],
    )
    print(f"Ingestion accepted: {result.job_id}")

    # 4. Get context
    context = await client.memory.get_context(
        user_id=user.id,
        query="programming preferences",
    )
    print(f"Context: {context}")

    # 5. Search graph
    results = await client.graph.search(
        user_id=user.id,
        query="preferences",
        types=["facts", "entities"],
    )
    for r in results:
        print(f"  {r.name}: {r.description}")

    # 6. Add business facts
    await client.facts.add(
        user_id=user.id,
        facts=[
            {"subject": user.id, "predicate": "purchased", "object": "Pro plan"},
        ],
    )

    # 7. List facts with pagination
    async for fact in client.facts.all(user_id=user.id):
        print(f"Fact: {fact.content}")

    await client.close()

asyncio.run(main())
```

---

## 16. Backward Compatibility — `model_config`

Every Pydantic response model MUST include:

```python
model_config = ConfigDict(extra="ignore")
```

This ensures that if the API adds new response fields in a future version, the SDK does not raise `ValidationError` on deserialisation. Unknown fields are silently dropped.

---

## 17. Testing

```python
# tests/test_memory.py
import pytest
import httpx
import respx
from OpenZep import AsyncMemGraph

@pytest.mark.asyncio
async def test_memory_ingest_success():
    with respx.mock:
        route = respx.post("https://test.local/v1/users/u1/memory").mock(
            return_value=httpx.Response(202, json={"job_id": "j_abc", "status": "accepted"})
        )

        client = AsyncMemGraph(api_key="test_key", base_url="https://test.local")
        result = await client.memory.ingest(
            user_id="u1",
            session_id="s1",
            messages=[{"role": "user", "content": "Hello"}],
        )

        assert result.job_id == "j_abc"
        assert route.called
        assert route.calls[0].request.headers["Authorization"] == "Bearer test_key"
        assert route.calls[0].request.headers["User-Agent"].startswith("OpenZep-py-sdk/")

    await client.close()
```

Run tests:

```bash
pytest tests/ -v --cov=OpenZep
```
