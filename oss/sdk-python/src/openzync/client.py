"""OpenZep client — sync and async entry points.

Usage::

    from openzync import AsyncOpenZep

    async with AsyncOpenZep(api_key="mg_live_...") as client:
        resp = await client.memory.ingest("project-id", messages=[{"role":"user","content":"Hello"}])
"""

from __future__ import annotations

import asyncio
from typing import Any

from openzync._http import AsyncHTTPTransport
from openzync.facts import AsyncFactsClient
from openzync.graph import AsyncGraphClient
from openzync.memory import AsyncMemoryClient
from openzync.projects import AsyncProjectsClient
from openzync.sessions import AsyncSessionsClient
from openzync.users import AsyncUsersClient


class AsyncOpenZep:
    """Async OpenZep client — primary implementation.

    All methods are async. Use within ``async def`` contexts.

    Args:
        api_key: The OpenZep API key.
        base_url: Base URL of the API server.
        timeout: Per-request timeout in seconds.

    Usage::

        async with AsyncOpenZep(api_key="...") as client:
            resp = await client.memory.ingest("project-id", messages=[...])
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "http://localhost:8000",
        timeout: float = 30.0,
    ) -> None:
        self._http = AsyncHTTPTransport(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
        )
        self.memory = AsyncMemoryClient(self._http)
        self.facts = AsyncFactsClient(self._http)
        self.graph = AsyncGraphClient(self._http)
        self.users = AsyncUsersClient(self._http)
        self.sessions = AsyncSessionsClient(self._http)
        self.projects = AsyncProjectsClient(self._http)

    async def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._http.close()

    async def __aenter__(self) -> AsyncOpenZep:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()


class OpenZep:
    """Sync OpenZep client — wraps ``AsyncOpenZep`` via ``asyncio.run()``.

    ⚠️  Not safe to use inside an existing event loop (Jupyter, async apps).
       For async environments, use ``AsyncOpenZep`` directly.

    Args:
        api_key: The OpenZep API key.
        base_url: Base URL of the API server.
        timeout: Per-request timeout in seconds.

    Usage::

        client = OpenZep(api_key="...")
        resp = client.memory.ingest("project-id", messages=[...])
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "http://localhost:8000",
        timeout: float = 30.0,
    ) -> None:
        self._async = AsyncOpenZep(api_key=api_key, base_url=base_url, timeout=timeout)
        self.memory = _SyncDomainWrapper(self._async.memory)
        self.facts = _SyncDomainWrapper(self._async.facts)
        self.graph = _SyncDomainWrapper(self._async.graph)
        self.users = _SyncDomainWrapper(self._async.users)
        self.sessions = _SyncDomainWrapper(self._async.sessions)
        self.projects = _SyncDomainWrapper(self._async.projects)

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        asyncio.run(self._async.close())


class _SyncDomainWrapper:
    """Wraps an async domain client, calling each method via ``asyncio.run()``."""

    def __init__(self, async_client: Any) -> None:
        self._async = async_client

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._async, name)

        if asyncio.iscoroutinefunction(attr):
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                return asyncio.run(attr(*args, **kwargs))
            return sync_wrapper

        return attr
