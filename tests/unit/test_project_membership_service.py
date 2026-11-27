"""Unit tests for ProjectMembershipService — email-based member onboarding.

Covers the ``add_by_email`` branches with mocked repositories (no database):
active user joins, duplicate membership, pending invite, registration-mode
gates (``open`` / ``invite_only`` / ``approval``), and email normalization.
HTTP status mapping (422/409/403/201, old-shape 422) is covered at the
router layer in ``tests/unit/routers/test_projects.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from core.exceptions import AuthorizationError, ConflictError, ValidationError
from schemas.auth import InviteResponse
from services.project_membership_service import ProjectMembershipService


@pytest.mark.unit
class TestAddByEmail:
    """``add_by_email`` unit tests — all collaborators are mocked."""

    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")
    INVITED_BY = UUID("00000000-0000-0000-0000-000000000002")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _make_service(
        self,
    ) -> tuple[ProjectMembershipService, AsyncMock, AsyncMock, AsyncMock, AsyncMock]:
        """Create the service with mocked repo, user, settings, and invite deps."""
        mock_repo = AsyncMock()
        mock_user_repo = AsyncMock()
        mock_settings_repo = AsyncMock()
        mock_invite_service = AsyncMock()
        service = ProjectMembershipService(
            repo=mock_repo,
            user_repo=mock_user_repo,
            settings_repo=mock_settings_repo,
            invite_service=mock_invite_service,
        )
        return service, mock_repo, mock_user_repo, mock_settings_repo, (
            mock_invite_service
        )

    def _make_mock_user(self, **kwargs: object) -> AsyncMock:
        """Mock a User row — only the fields ``add_by_email`` reads."""
        user = AsyncMock()
        user.id = kwargs.get("id", uuid4())
        user.invite_token_hash = kwargs.get("invite_token_hash")
        user.is_active = kwargs.get("is_active", True)
        return user

    def _make_mock_member(self, **kwargs: object) -> AsyncMock:
        """Mock a ProjectMember row for ``_to_response``."""
        member = AsyncMock()
        member.id = kwargs.get("id", uuid4())
        member.project_id = kwargs.get("project_id", self.PROJECT_ID)
        member.user_id = kwargs.get("user_id", uuid4())
        member.role = kwargs.get("role", "member")
        member.created_at = kwargs.get("created_at", datetime.now(UTC))
        return member

    # ── Active user joins ────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_add_active_user_returns_member(self) -> None:
        """Active, non-member user is attached directly (HTTP 201)."""
        service, mock_repo, mock_user_repo, _, _ = self._make_service()
        user = self._make_mock_user()
        mock_user_repo.find_user_by_email.return_value = user
        mock_repo.get_member.return_value = None
        mock_repo.add_member.return_value = self._make_mock_member(
            user_id=user.id,
            role="member",
        )

        result = await service.add_by_email(
            project_id=self.PROJECT_ID,
            email="alice@example.com",
            role="member",
            invited_by=self.INVITED_BY,
        )

        assert result.user_id == user.id
        assert result.role == "member"
        mock_repo.add_member.assert_awaited_once_with(
            project_id=self.PROJECT_ID,
            user_id=user.id,
            role="member",
        )

    # ── Already a member ─────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_add_existing_member_raises_validation(self) -> None:
        """Duplicate membership raises ValidationError (HTTP 422 already_member)."""
        service, mock_repo, mock_user_repo, _, _ = self._make_service()
        user = self._make_mock_user()
        mock_user_repo.find_user_by_email.return_value = user
        mock_repo.get_member.return_value = self._make_mock_member(user_id=user.id)

        with pytest.raises(ValidationError) as exc_info:
            await service.add_by_email(
                project_id=self.PROJECT_ID,
                email="alice@example.com",
                role="member",
                invited_by=self.INVITED_BY,
            )

        assert exc_info.value.detail["code"] == "already_member"
        mock_repo.add_member.assert_not_awaited()

    # ── Pending invite ───────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_add_pending_invite_raises_conflict(self) -> None:
        """User with an outstanding invite raises ConflictError (HTTP 409)."""
        service, mock_repo, mock_user_repo, _, _ = self._make_service()
        user = self._make_mock_user(invite_token_hash="hashed-token")
        mock_user_repo.find_user_by_email.return_value = user
        mock_repo.get_member.return_value = None

        with pytest.raises(ConflictError) as exc_info:
            await service.add_by_email(
                project_id=self.PROJECT_ID,
                email="alice@example.com",
                role="member",
                invited_by=self.INVITED_BY,
            )

        assert exc_info.value.detail["code"] == "invite_pending"
        mock_repo.add_member.assert_not_awaited()

    # ── Unknown email: open mode invites and adds ────────────────────────────

    @pytest.mark.asyncio
    async def test_add_unknown_email_open_invites_and_adds(self) -> None:
        """Unknown email under ``open`` mode is invited, then added (HTTP 201)."""
        service, mock_repo, mock_user_repo, mock_settings_repo, mock_invite = (
            self._make_service()
        )
        mock_user_repo.find_user_by_email.return_value = None
        mock_settings_repo.get_registration_mode.return_value = "open"
        pending_id = uuid4()
        mock_invite.invite_user.return_value = InviteResponse(
            id=pending_id,
            email="new@example.com",
            name="new",
        )
        mock_repo.add_member.return_value = self._make_mock_member(
            user_id=pending_id,
            role="member",
        )

        result = await service.add_by_email(
            project_id=self.PROJECT_ID,
            email="new@example.com",
            role="member",
            invited_by=self.INVITED_BY,
        )

        assert result.user_id == pending_id
        assert result.role == "member"
        mock_invite.invite_user.assert_awaited_once()
        mock_repo.add_member.assert_awaited_once_with(
            project_id=self.PROJECT_ID,
            user_id=pending_id,
            role="member",
        )

    # ── Unknown email: invite-only rejects ───────────────────────────────────

    @pytest.mark.asyncio
    async def test_add_unknown_email_invite_only_raises_authorization(self) -> None:
        """Unknown email under ``invite_only`` raises AuthorizationError (HTTP 403)."""
        service, mock_repo, mock_user_repo, mock_settings_repo, mock_invite = (
            self._make_service()
        )
        mock_user_repo.find_user_by_email.return_value = None
        mock_settings_repo.get_registration_mode.return_value = "invite_only"

        with pytest.raises(AuthorizationError) as exc_info:
            await service.add_by_email(
                project_id=self.PROJECT_ID,
                email="new@example.com",
                role="member",
                invited_by=self.INVITED_BY,
            )

        assert exc_info.value.detail["code"] == "registration_invite_only"
        mock_invite.invite_user.assert_not_awaited()
        mock_repo.add_member.assert_not_awaited()

    # ── Unknown email: approval rejects ──────────────────────────────────────

    @pytest.mark.asyncio
    async def test_add_unknown_email_approval_raises_validation(self) -> None:
        """Unknown email under ``approval`` raises ValidationError (HTTP 422)."""
        service, mock_repo, mock_user_repo, mock_settings_repo, mock_invite = (
            self._make_service()
        )
        mock_user_repo.find_user_by_email.return_value = None
        mock_settings_repo.get_registration_mode.return_value = "approval"

        with pytest.raises(ValidationError) as exc_info:
            await service.add_by_email(
                project_id=self.PROJECT_ID,
                email="new@example.com",
                role="member",
                invited_by=self.INVITED_BY,
            )

        assert exc_info.value.detail["code"] == "registration_approval_required"
        mock_invite.invite_user.assert_not_awaited()
        mock_repo.add_member.assert_not_awaited()

    # ── Normalization ────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_email_is_stripped_and_lowercased(self) -> None:
        """Leading/trailing space and case are normalized before lookup."""
        service, mock_repo, mock_user_repo, _, _ = self._make_service()
        user = self._make_mock_user()
        mock_user_repo.find_user_by_email.return_value = user
        mock_repo.get_member.return_value = None
        mock_repo.add_member.return_value = self._make_mock_member(user_id=user.id)

        await service.add_by_email(
            project_id=self.PROJECT_ID,
            email="  Alice@Example.COM  ",
            role="member",
            invited_by=self.INVITED_BY,
        )

        mock_user_repo.find_user_by_email.assert_awaited_once_with(
            "alice@example.com"
        )
