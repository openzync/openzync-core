"""Projects domain client — CRUD operations and member management."""

from __future__ import annotations

from openzync._http import AsyncHTTPTransport
from openzync._pagination import AsyncPaginatedIterator
from openzync.models.project import (
    AddMemberRequest,
    CreateProjectRequest,
    ProjectMemberResponse,
    ProjectResponse,
    UpdateProjectRequest,
)


class AsyncProjectsClient:
    """Async client for project operations.

    Args:
        http: The shared async HTTP transport instance.
    """

    def __init__(self, http: AsyncHTTPTransport) -> None:
        self._http = http

    async def create(
        self,
        name: str,
        description: str | None = None,
        metadata: dict | None = None,
    ) -> ProjectResponse:
        """Create a new project.

        The authenticated user is automatically added as an ``owner``.

        Args:
            name: Display name for the project (1-255 chars).
            description: Optional description.
            metadata: Optional metadata dict.

        Returns:
            ``ProjectResponse`` with the created project.
        """
        body = CreateProjectRequest(
            name=name,
            description=description,
            metadata=metadata if metadata is not None else {},
        )
        data = await self._http.request(
            "POST",
            "/v1/projects",
            json_body=body.model_dump(exclude_none=True),
        )
        return ProjectResponse(**data)

    async def get(self, project_id: str) -> ProjectResponse:
        """Get project details by UUID.

        Args:
            project_id: The internal UUID of the project.

        Returns:
            ``ProjectResponse`` with project details including member count.
        """
        data = await self._http.request(
            "GET",
            f"/v1/projects/{project_id}",
        )
        return ProjectResponse(**data)

    async def update(
        self,
        project_id: str,
        name: str | None = None,
        description: str | None = None,
        metadata: dict | None = None,
        is_archived: bool | None = None,
    ) -> ProjectResponse:
        """Update project fields.

        Args:
            project_id: The internal UUID of the project.
            name: Optional new display name.
            description: Optional new description.
            metadata: Optional new metadata dict.
            is_archived: Optional archive flag.

        Returns:
            ``ProjectResponse`` with updated fields.
        """
        body = UpdateProjectRequest(
            name=name,
            description=description,
            metadata=metadata,
            is_archived=is_archived,
        )
        data = await self._http.request(
            "PUT",
            f"/v1/projects/{project_id}",
            json_body=body.model_dump(exclude_none=True),
        )
        return ProjectResponse(**data)

    async def list(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        """List projects with offset-based pagination.

        Args:
            limit: Maximum results per page.
            cursor: Opaque cursor from a previous response (offset value).

        Returns:
            Dict with ``data``, ``next_cursor``, and ``has_more`` keys.
        """
        params: dict[str, str | int] = {"limit": limit, "offset": 0}
        if cursor is not None:
            params["offset"] = int(cursor)
        data = await self._http.request(
            "GET",
            "/v1/projects",
            params=params,
        )
        # Backend returns a flat array — wrap in paginated envelope
        if isinstance(data, list):
            return {"data": data, "next_cursor": None, "has_more": False}
        return data

    def list_iter(self, *, limit: int = 50) -> AsyncPaginatedIterator:
        """Iterate over all projects with auto-pagination.

        Usage::

            async for project in client.projects.list_iter():
                print(project["name"])
        """
        async def fetch_page(cursor: str | None = None) -> dict:
            return await self.list(limit=limit, cursor=cursor)

        return AsyncPaginatedIterator(fetch_page, limit)

    async def archive(self, project_id: str) -> None:
        """Archive (soft-delete) a project.

        Args:
            project_id: The internal UUID of the project.
        """
        await self._http.request(
            "DELETE",
            f"/v1/projects/{project_id}",
        )

    async def add_member(
        self,
        project_id: str,
        user_id: str,
        role: str = "member",
    ) -> ProjectMemberResponse:
        """Add a member to a project.

        Args:
            project_id: The internal UUID of the project.
            user_id: The UUID of the user to add.
            role: Project role (``"owner"`` or ``"member"``).

        Returns:
            ``ProjectMemberResponse`` with membership details.
        """
        body = AddMemberRequest(user_id=user_id, role=role)
        data = await self._http.request(
            "POST",
            f"/v1/projects/{project_id}/members",
            json_body=body.model_dump(exclude_none=True),
        )
        return ProjectMemberResponse(**data)

    async def remove_member(self, project_id: str, user_id: str) -> None:
        """Remove a member from a project.

        Args:
            project_id: The internal UUID of the project.
            user_id: The UUID of the user to remove.
        """
        await self._http.request(
            "DELETE",
            f"/v1/projects/{project_id}/members/{user_id}",
        )

    async def list_members(
        self,
        project_id: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        """List members of a project.

        Args:
            project_id: The internal UUID of the project.
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
            f"/v1/projects/{project_id}/members",
            params=params,
        )
