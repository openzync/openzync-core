"""Sessions domain client — CRUD operations."""

from __future__ import annotations

from openzep._http import AsyncHTTPTransport
from openzep._pagination import AsyncPaginatedIterator
from openzep.models.session import (
    SessionCreateRequest,
    SessionMessagesResponse,
    SessionResponse,
)


class AsyncSessionsClient:
    """Async client for session operations.

    Args:
        http: The shared async HTTP transport instance.
    """

    def __init__(self, http: AsyncHTTPTransport) -> None:
        self._http = http

    async def create(
        self,
        user_id: str,
        external_id: str,
        metadata: dict | None = None,
    ) -> SessionResponse:
        """Create a new session for a user.

        Args:
            user_id: The internal UUID of the user.
            external_id: Caller-defined session identifier.
            metadata: Optional metadata dict.

        Returns:
            ``SessionResponse`` with the created session.
        """
        body = SessionCreateRequest(external_id=external_id, metadata=metadata or {})
        data = await self._http.request(
            "POST",
            f"/v1/users/{user_id}/sessions",
            json_body=body.model_dump(exclude_none=True),
        )
        return SessionResponse(**data)

    async def get(
        self,
        user_id: str,
        session_id: str,
    ) -> SessionResponse:
        """Get session details by internal UUID.

        Args:
            user_id: The internal UUID of the user.
            session_id: The internal UUID of the session.
        """
        data = await self._http.request(
            "GET",
            f"/v1/users/{user_id}/sessions/{session_id}",
        )
        return SessionResponse(**data)

    async def delete(
        self,
        user_id: str,
        session_id: str,
    ) -> None:
        """Close and soft-delete a session.

        Args:
            user_id: The internal UUID of the user.
            session_id: The internal UUID of the session.
        """
        await self._http.request(
            "DELETE",
            f"/v1/users/{user_id}/sessions/{session_id}",
        )

    async def list(
        self,
        user_id: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        """List sessions for a user with cursor-based pagination.

        Args:
            user_id: The internal UUID of the user.
            limit: Maximum results per page.
            cursor: Opaque cursor from a previous response.

        Returns:
            Dict with ``data``, ``next_cursor``, and ``has_more`` keys.
        """
        params: dict[str, str | int] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        return await self._http.request(
            "GET",
            f"/v1/users/{user_id}/sessions",
            params=params,
        )

    async def messages(
        self,
        user_id: str,
        session_id: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> SessionMessagesResponse:
        """Get messages for a session.

        Args:
            user_id: The internal UUID of the user.
            session_id: The internal UUID of the session.
            limit: Maximum results per page.
            cursor: Opaque cursor from a previous response.

        Returns:
            ``SessionMessagesResponse`` with message list.
        """
        params: dict[str, str | int] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        data = await self._http.request(
            "GET",
            f"/v1/users/{user_id}/sessions/{session_id}/messages",
            params=params,
        )
        return SessionMessagesResponse(**data)
