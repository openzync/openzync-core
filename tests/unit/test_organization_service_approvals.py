"""Unit tests for OrganizationService.approve_org / reject_org.

All external IO (repo, OpenBao, email) is mocked at the service boundary.

Observed contract:
- approve: pending → approved, OpenBao namespace created (+ defaults),
  invite token minted on the pending admin, invite email sent.
- reject:  pending → rejected, no email.
- wrong status (approved/rejected org) → ConflictError (409) on both.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from core.exceptions import ConflictError
from services.organization_service import OrganizationService

pytestmark = pytest.mark.unit

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
ADMIN_USER_ID = UUID("00000000-0000-0000-0000-000000000002")
ACTOR_ID = UUID("00000000-0000-0000-0000-0000000000aa")


class TestOrganizationApprovals:
    """approve_org / reject_org unit tests."""

    def _make_service(
        self,
        bao_client: AsyncMock | None = None,
        email_service: AsyncMock | None = None,
    ) -> tuple[OrganizationService, AsyncMock, AsyncMock]:
        """Create ``OrganizationService`` with mocked repo, db, and deps."""
        mock_repo = AsyncMock()
        mock_db = AsyncMock()
        mock_repo.session = mock_db
        service = OrganizationService(
            repo=mock_repo,
            bao_client=bao_client,
            email_service=email_service,
        )
        return service, mock_repo, mock_db

    def _make_org(self, status: str = "pending") -> MagicMock:
        """Build a MagicMock mimicking an Organization ORM instance."""
        org = MagicMock()
        org.id = ORG_ID
        org.name = "Acme Corp"
        org.plan = "free"
        org.status = status
        org.org_code = "K7M2Q9X4"
        return org

    def _make_pending_admin(self) -> MagicMock:
        """Build a MagicMock mimicking the pending admin User."""
        admin = MagicMock()
        admin.id = ADMIN_USER_ID
        admin.organization_id = ORG_ID
        admin.email = "admin@acme.com"
        admin.name = "Admin"
        return admin

    # ── approve_org ─────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_approve_flips_status_and_invites_admin(self) -> None:
        """Approve: status→approved, namespace created, invite token set,
        invite email sent."""
        mock_bao = AsyncMock()
        mock_email = AsyncMock()
        service, mock_repo, mock_db = self._make_service(
            bao_client=mock_bao, email_service=mock_email
        )
        org = self._make_org(status="pending")
        admin = self._make_pending_admin()
        mock_repo.get_by_id.return_value = org
        mock_repo.approve_if_pending.return_value = True
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        with (
            patch("services.organization_service.UserRepository") as mock_user_repo_cls,
            patch(
                "services.invite_service._hash_invite_token",
                return_value="hashed-token",
            ),
            patch(
                "services.organization_service.secrets.token_urlsafe",
                return_value="raw-token",
            ),
            patch(
                "services.invite_service.send_invite_email",
                new=AsyncMock(),
            ) as mock_send_email,
            patch.object(
                service,
                "_load_org_defaults",
                return_value={"llm_model": "gpt-4o-mini"},
            ),
        ):
            mock_user_repo = AsyncMock()
            mock_user_repo.find_pending_admin_by_org.return_value = admin
            mock_user_repo.set_invite_token.return_value = admin
            mock_user_repo_cls.return_value = mock_user_repo

            result = await service.approve_org(ORG_ID, ACTOR_ID)

        assert result.status == "approved"
        mock_repo.approve_if_pending.assert_awaited_once_with(ORG_ID)
        mock_user_repo.find_pending_admin_by_org.assert_awaited_once_with(ORG_ID)
        mock_user_repo.set_invite_token.assert_awaited_once_with(
            organization_id=ORG_ID,
            user_id=ADMIN_USER_ID,
            token_hash="hashed-token",  # noqa: S106 — test fixture token
        )
        mock_bao.create_org_namespace.assert_awaited_once_with(ORG_ID)
        mock_bao.write_org_config.assert_awaited_once_with(
            ORG_ID, {"llm_model": "gpt-4o-mini"}
        )
        # Invite email sent via the shared sender with the raw magic-link token.
        mock_send_email.assert_awaited_once_with(
            mock_email,
            org_name="Acme Corp",
            inviter_name="A platform administrator",
            invitee_name="Admin",
            invitee_email="admin@acme.com",
            raw_token="raw-token",  # noqa: S106 — test fixture token  # noqa: S106 — test fixture token
        )

    @pytest.mark.asyncio
    async def test_approve_wrong_status_raises_conflict(self) -> None:
        """Approving an already-approved org → ConflictError."""
        service, mock_repo, _ = self._make_service()
        mock_repo.get_by_id.return_value = self._make_org(status="approved")

        with pytest.raises(ConflictError):
            await service.approve_org(ORG_ID, ACTOR_ID)

    @pytest.mark.asyncio
    async def test_concurrent_approve_loser_gets_conflict_no_second_invite(
        self,
    ) -> None:
        """Two concurrent approvals: only one mints the invite.

        The loser's atomic claim (conditional UPDATE on status='pending')
        matches zero rows once the winner's flip commits — the loser raises
        ConflictError BEFORE any side effect: no second invite token, no
        duplicate approval email, no namespace re-bootstrap.
        """
        mock_bao = AsyncMock()
        mock_email = AsyncMock()
        service, mock_repo, _ = self._make_service(
            bao_client=mock_bao, email_service=mock_email
        )
        # The winner already flipped the org: the loser's claim misses.
        mock_repo.approve_if_pending.return_value = False
        mock_repo.get_by_id.return_value = self._make_org(status="approved")

        with (
            patch("services.organization_service.UserRepository") as mock_user_repo_cls,
            patch(
                "services.invite_service.send_invite_email",
                new=AsyncMock(),
            ) as mock_send_email,
        ):
            mock_user_repo = AsyncMock()
            mock_user_repo_cls.return_value = mock_user_repo

            with pytest.raises(ConflictError):
                await service.approve_org(ORG_ID, ACTOR_ID)

        # No invite minted, no email, no OpenBao work — the side effects
        # run only for the single winner.
        mock_user_repo.find_pending_admin_by_org.assert_not_awaited()
        mock_user_repo.set_invite_token.assert_not_awaited()
        mock_send_email.assert_not_awaited()
        mock_bao.create_org_namespace.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_approve_no_pending_admin_raises_conflict(self) -> None:
        """A pending org with no pending admin row → ConflictError."""
        service, mock_repo, _ = self._make_service()
        mock_repo.get_by_id.return_value = self._make_org(status="pending")

        with (
            patch("services.organization_service.UserRepository") as mock_user_repo_cls,
        ):
            mock_user_repo = AsyncMock()
            mock_user_repo.find_pending_admin_by_org.return_value = None
            mock_user_repo_cls.return_value = mock_user_repo
            with pytest.raises(ConflictError):
                await service.approve_org(ORG_ID, ACTOR_ID)

    @pytest.mark.asyncio
    async def test_approve_openbao_failure_rolls_back(self) -> None:
        """OpenBao namespace failure propagates — status flip is not kept."""
        mock_bao = AsyncMock()
        mock_bao.create_org_namespace.side_effect = RuntimeError("bao down")
        service, mock_repo, mock_db = self._make_service(bao_client=mock_bao)
        org = self._make_org(status="pending")
        admin = self._make_pending_admin()
        mock_repo.get_by_id.return_value = org

        with (
            patch("services.organization_service.UserRepository") as mock_user_repo_cls,
            patch.object(
                service,
                "_load_org_defaults",
                return_value={},
            ),
        ):
            mock_user_repo = AsyncMock()
            mock_user_repo.find_pending_admin_by_org.return_value = admin
            mock_user_repo_cls.return_value = mock_user_repo

            with pytest.raises(RuntimeError):
                await service.approve_org(ORG_ID, ACTOR_ID)

        # The failure propagates — the request-scoped bypass session rolls
        # the status flip back (the service never swallows it).
        mock_bao.create_org_namespace.assert_awaited_once_with(ORG_ID)

    # ── reject_org ──────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_reject_flips_status_no_email(self) -> None:
        """Reject: pending → rejected, no email is sent."""
        mock_email = AsyncMock()
        service, mock_repo, _ = self._make_service(email_service=mock_email)
        org = self._make_org(status="pending")
        mock_repo.get_by_id.return_value = org

        result = await service.reject_org(ORG_ID, ACTOR_ID)

        assert result.status == "rejected"
        mock_email.send_email.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reject_wrong_status_raises_conflict(self) -> None:
        """Rejecting a rejected org → ConflictError."""
        service, mock_repo, _ = self._make_service()
        mock_repo.get_by_id.return_value = self._make_org(status="rejected")

        with pytest.raises(ConflictError):
            await service.reject_org(ORG_ID, ACTOR_ID)
