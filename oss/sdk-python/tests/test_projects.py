"""Tests for the OpenZep Python SDK — projects domain."""

from __future__ import annotations

import pytest

from openzync.models.project import ProjectMemberResponse, ProjectResponse


class TestProjectsClient:
    """Tests for ``AsyncProjectsClient``."""

    @pytest.mark.asyncio
    async def test_create_project(self, async_client, mock_http):
        """POST /projects returns ProjectResponse."""
        mock_http.post("/v1/projects").respond(json={
            "id": "p1",
            "name": "My Project",
            "description": "A test project",
            "metadata": {},
            "is_archived": False,
            "member_count": 1,
            "created_by": "u1",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        })

        project = await async_client.projects.create(
            name="My Project",
            description="A test project",
        )
        assert isinstance(project, ProjectResponse)
        assert project.id == "p1"
        assert project.name == "My Project"
        assert project.member_count == 1
        assert project.created_by == "u1"

    @pytest.mark.asyncio
    async def test_get_project(self, async_client, mock_http):
        """GET /projects/{id} returns project."""
        project_id = "p1"
        mock_http.get(f"/v1/projects/{project_id}").respond(json={
            "id": project_id,
            "name": "My Project",
            "description": None,
            "metadata": {},
            "is_archived": False,
            "member_count": 2,
            "created_by": "u1",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        })

        project = await async_client.projects.get(project_id=project_id)
        assert project.id == project_id
        assert project.member_count == 2

    @pytest.mark.asyncio
    async def test_list_projects(self, async_client, mock_http):
        """GET /projects returns paginated results."""
        mock_http.get("/v1/projects").respond(json={
            "data": [
                {"id": "p1", "name": "Project 1", "metadata": {},
                 "is_archived": False, "member_count": 1, "created_by": "u1",
                 "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"},
                {"id": "p2", "name": "Project 2", "metadata": {},
                 "is_archived": False, "member_count": 3, "created_by": "u2",
                 "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"},
            ],
            "next_cursor": None,
            "has_more": False,
        })

        result = await async_client.projects.list()
        assert len(result["data"]) == 2
        assert result["data"][0]["name"] == "Project 1"

    @pytest.mark.asyncio
    async def test_update_project(self, async_client, mock_http):
        """PUT /projects/{id} updates and returns project."""
        project_id = "p1"
        mock_http.put(f"/v1/projects/{project_id}").respond(json={
            "id": project_id,
            "name": "Updated Name",
            "description": "Updated desc",
            "metadata": {"key": "val"},
            "is_archived": False,
            "member_count": 1,
            "created_by": "u1",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
        })

        project = await async_client.projects.update(
            project_id=project_id,
            name="Updated Name",
        )
        assert project.name == "Updated Name"
        assert project.metadata == {"key": "val"}

    @pytest.mark.asyncio
    async def test_archive_project(self, async_client, mock_http):
        """DELETE /projects/{id} returns 204."""
        project_id = "p1"
        mock_http.delete(f"/v1/projects/{project_id}").respond(status_code=204)

        await async_client.projects.archive(project_id=project_id)
        # No exception means success

    @pytest.mark.asyncio
    async def test_add_member(self, async_client, mock_http):
        """POST /projects/{id}/members adds a member."""
        project_id = "p1"
        mock_http.post(f"/v1/projects/{project_id}/members").respond(json={
            "id": "m1",
            "project_id": project_id,
            "user_id": "u2",
            "role": "member",
            "created_at": "2026-01-01T00:00:00Z",
        })

        member = await async_client.projects.add_member(
            project_id=project_id,
            user_id="u2",
            role="member",
        )
        assert isinstance(member, ProjectMemberResponse)
        assert member.user_id == "u2"
        assert member.role == "member"

    @pytest.mark.asyncio
    async def test_remove_member(self, async_client, mock_http):
        """DELETE /projects/{id}/members/{user_id} returns 204."""
        project_id = "p1"
        user_id = "u2"
        mock_http.delete(f"/v1/projects/{project_id}/members/{user_id}").respond(
            status_code=204
        )

        await async_client.projects.remove_member(
            project_id=project_id,
            user_id=user_id,
        )

    @pytest.mark.asyncio
    async def test_list_members(self, async_client, mock_http):
        """GET /projects/{id}/members returns members."""
        project_id = "p1"
        mock_http.get(f"/v1/projects/{project_id}/members").respond(json={
            "data": [
                {"id": "m1", "project_id": project_id, "user_id": "u1",
                 "role": "owner", "created_at": "2026-01-01T00:00:00Z"},
                {"id": "m2", "project_id": project_id, "user_id": "u2",
                 "role": "member", "created_at": "2026-01-01T00:00:00Z"},
            ],
            "next_cursor": None,
            "has_more": False,
        })

        result = await async_client.projects.list_members(project_id=project_id)
        assert len(result["data"]) == 2
        assert result["data"][0]["role"] == "owner"
