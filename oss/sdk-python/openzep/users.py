"""Users domain client — CRUD operations."""

from __future__ import annotations

from openzep._http import AsyncHTTPTransport
from openzep._pagination import AsyncPaginatedIterator
from openzep.models.user import (
    UserCreateRequest,
    UserResponse,
    UserUpdateRequest,
)


class AsyncUsersClient:
    """Async client for user operations.

    Args:
        http: The shared async HTTP transport instance.
    """

    def __init__(self, http: AsyncHTTPTransport) -> None:
        self._http = http

    async def create(
        self,
        external_id: str,
        name: str | None = None,
        email: str | None = None,
        metadata: dict | None = None,
    ) -> UserResponse:
        """Create a new user.

        Args:
            external_id: Caller-defined user identifier.
            name: Optional display name.
            email: Optional email address.
            metadata: Optional metadata dict.

        Returns:
            ``UserResponse`` with the created user.
        """
        body = UserCreateRequest(
            external_id=external_id,
            name=name,
            email=email,
            metadata=metadata or {},
        )
        data = await self._http.request(
            "POST",
            "/v1/users",
            json_body=body.model_dump(exclude_none=True),
        )
        return UserResponse(**data)

    async def get(self, user_id: str) -> UserResponse:
        """Get user details by internal UUID.

        Args:
            user_id: The internal UUID of the user.

        Returns:
            ``UserResponse`` with user details including counts.
        """
        data = await self._http.request("GET", f"/v1/users/{user_id}")
        return UserResponse(**data)

    async def update(
        self,
        user_id: str,
        name: str | None = None,
        email: str | None = None,
        metadata: dict | None = None,
    ) -> UserResponse:
        """Update user fields.

        Args:
            user_id: The internal UUID of the user.
            name: Optional new display name.
            email: Optional new email.
            metadata: Optional new metadata dict.

        Returns:
            ``UserResponse`` with updated fields.
        """
        body = UserUpdateRequest(name=name, email=email, metadata=metadata)
        data = await self._http.request(
            "PATCH",
            f"/v1/users/{user_id}",
            json_body=body.model_dump(exclude_none=True),
        )
        return UserResponse(**data)

    async def delete(self, user_id: str) -> None:
        """Soft-delete a user.

        Args:
            user_id: The internal UUID of the user.
        """
        await self._http.request("DELETE", f"/v1/users/{user_id}")

    async def list(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        """List users with cursor-based pagination.

        Args:
            limit: Maximum results per page.
            cursor: Opaque cursor from a previous response.

        Returns:
            Dict with ``data``, ``next_cursor``, and ``has_more`` keys.
        """
        params: dict[str, str | int] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        return await self._http.request("GET", "/v1/users", params=params)

    def list_iter(self, *, limit: int = 50) -> AsyncPaginatedIterator:
        """Iterate over all users with auto-pagination.

        Usage::

            async for user in client.users.list_iter():
                print(user["name"])
        """
        async def fetch_page(cursor: str | None = None) -> dict:
            return await self.list(limit=limit, cursor=cursor)

        return AsyncPaginatedIterator(fetch_page, limit)
