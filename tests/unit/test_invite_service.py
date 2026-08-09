"""Unit tests for ``InviteService`` — the admin invite-by-email flow.

All external IO (user repository, org repository, auth service, email
service, password hashing) is mocked at the service boundary.  The email
template renderers are real (they read the checked-in ``prompts/email/*``
files), so the happy path also pins the template context keys.

Observed contract:
- invite: duplicate email → loud 409; missing org/admin → 404; row created
  with ``password_hash=None`` + token hash BEFORE the email is sent; send
  failure propagates ``ExternalServiceError`` (the session dependency rolls
  the row back — never commit-then-send); response never carries the token.
- revoke: hard-deletes only pending rows in the org; 404 otherwise.
- info: generic 404 for unknown/expired/used; expired rows self-cleaned.
- accept: atomic claim via ``claim_invite`` (no select-then-update), then
  ``issue_tokens`` from the RETURNING row; miss → generic 404 with
  self-clean of expired-but-present rows; replay → 404.
"""

# ruff: noqa: S105, S106  — every fixture here IS a token/password by design

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from core.exceptions import ConflictError, ExternalServiceError, NotFoundError
from repositories.organization_repository import OrganizationRepository
from repositories.user_repository import UserRepository
from schemas.auth import InviteRequest, InviteResponse, TokenResponse
from services.auth_service import AuthService
from services.email_service import EmailService
from services.invite_service import INVITE_EXPIRY_HOURS, InviteService

pytestmark = pytest.mark.unit

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
ADMIN_ID = UUID("00000000-0000-0000-0000-000000000002")
INVITEE_ID = UUID("00000000-0000-0000-0000-000000000003")
OTHER_USER_ID = UUID("00000000-0000-0000-0000-000000000004")

FAKE_TOKEN_RESPONSE = TokenResponse(
    access_token="access.jwt.token",
    refresh_token="raw-refresh-token",
    expires_in=1800,
)


def _invite_request(
    email: str = "alice@acme.com",
    name: str = "Alice Johnson",
) -> InviteRequest:
    """Build a valid invite request."""
    return InviteRequest(email=email, name=name)


def _make_org(name: str = "Acme Corp") -> AsyncMock:
    org = AsyncMock()
    org.id = ORG_ID
    org.name = name
    return org


def _make_admin(name: str = "Boss Admin") -> AsyncMock:
    admin = AsyncMock()
    admin.id = ADMIN_ID
    admin.name = name
    admin.email = "boss@acme.com"
    return admin


def _make_pending_user(
    *,
    user_id: UUID = INVITEE_ID,
    org_id: UUID = ORG_ID,
    email: str = "alice@acme.com",
    name: str = "Alice Johnson",
    created_at: datetime | None = None,
) -> AsyncMock:
    user = AsyncMock()
    user.id = user_id
    user.organization_id = org_id
    user.email = email
    user.name = name
    user.created_at = created_at or datetime.now(UTC)
    return user


class TestInviteService:
    """InviteService unit tests."""

    @pytest.fixture
    def mock_repo(self) -> AsyncMock:
        return AsyncMock(spec=UserRepository)

    @pytest.fixture
    def mock_auth_service(self) -> AsyncMock:
        auth = AsyncMock(spec=AuthService)
        auth.issue_tokens = AsyncMock(return_value=FAKE_TOKEN_RESPONSE)
        return auth

    @pytest.fixture
    def mock_email_service(self) -> AsyncMock:
        return AsyncMock(spec=EmailService)

    @pytest.fixture
    def mock_org_repo(self) -> AsyncMock:
        return AsyncMock(spec=OrganizationRepository)

    @pytest.fixture
    def service(
        self,
        mock_repo: AsyncMock,
        mock_auth_service: AsyncMock,
        mock_email_service: AsyncMock,
        mock_org_repo: AsyncMock,
    ) -> InviteService:
        return InviteService(
            repo=mock_repo,
            auth_service=mock_auth_service,
            email_service=mock_email_service,
            org_repo=mock_org_repo,
        )

    # ── invite_user ─────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_invite_happy_path_creates_row_sends_email_no_token(
        self,
        service: InviteService,
        mock_repo: AsyncMock,
        mock_email_service: AsyncMock,
        mock_org_repo: AsyncMock,
    ) -> None:
        """Valid invite → pending row created, email sent, no token in response."""
        mock_repo.find_user_by_email.return_value = None
        mock_org_repo.get_by_id.return_value = _make_org()
        mock_repo.get_by_uuid.return_value = _make_admin()
        mock_repo.create.return_value = _make_pending_user()

        with patch(
            "services.invite_service.secrets.token_urlsafe", return_value="raw-token",
        ):
            result = await service.invite_user(
                admin_user_id=ADMIN_ID,
                org_id=ORG_ID,
                payload=_invite_request(),
            )

        assert isinstance(result, InviteResponse)
        assert result.id == INVITEE_ID
        assert result.email == "alice@acme.com"
        assert result.name == "Alice Johnson"
        assert not hasattr(result, "token")

        # Duplicate check runs before anything is created
        mock_repo.find_user_by_email.assert_awaited_once_with("alice@acme.com")

        # Row created with password_hash=None + SHA-256 of the raw token
        create_kwargs = mock_repo.create.call_args.kwargs
        assert create_kwargs["organization_id"] == ORG_ID
        assert create_kwargs["email"] == "alice@acme.com"
        assert create_kwargs["role"] == "member"
        assert create_kwargs["password_hash"] is None
        # sha256("raw-token") — the token is stored hashed, never plaintext
        assert create_kwargs["invite_token_hash"] == (
            "34d328009b123fbbb0dc93f18b3e6de1ecf7b1a5783c33dff7ffe1926f09e943"
        )

        # Email sent AFTER the row is created, with the magic-link URL
        mock_email_service.send_email.assert_awaited_once()
        send_kwargs = mock_email_service.send_email.call_args.kwargs
        assert send_kwargs["to"] == "alice@acme.com"
        assert send_kwargs["subject"] == "You've been invited to Acme Corp"
        link = "http://localhost:3000/invite?token=raw-token"
        assert link in send_kwargs["html_body"]
        assert link in send_kwargs["text_body"]

    @pytest.mark.asyncio
    async def test_invite_duplicate_email_raises_conflict(
        self,
        service: InviteService,
        mock_repo: AsyncMock,
        mock_email_service: AsyncMock,
        mock_org_repo: AsyncMock,
    ) -> None:
        """Existing account with this email → loud 409, nothing created/sent."""
        mock_repo.find_user_by_email.return_value = _make_pending_user()

        with pytest.raises(ConflictError) as exc:
            await service.invite_user(
                admin_user_id=ADMIN_ID,
                org_id=ORG_ID,
                payload=_invite_request(),
            )

        assert str(exc.value) == "An account with this email already exists."
        mock_repo.create.assert_not_awaited()
        mock_org_repo.get_by_id.assert_not_awaited()
        mock_email_service.send_email.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invite_missing_org_raises_not_found(
        self,
        service: InviteService,
        mock_repo: AsyncMock,
        mock_org_repo: AsyncMock,
    ) -> None:
        """Org deleted since login → loud 404, no row created."""
        mock_repo.find_user_by_email.return_value = None
        mock_org_repo.get_by_id.return_value = None

        with pytest.raises(NotFoundError):
            await service.invite_user(
                admin_user_id=ADMIN_ID,
                org_id=ORG_ID,
                payload=_invite_request(),
            )

        mock_repo.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invite_send_failure_propagates_row_rolled_back(
        self,
        service: InviteService,
        mock_repo: AsyncMock,
        mock_email_service: AsyncMock,
        mock_org_repo: AsyncMock,
    ) -> None:
        """Send failure → ExternalServiceError propagates (502 via handler).

        The row was created but never committed — the request-scoped
        ``get_db`` dependency rolls the transaction back on the raised
        exception (``dependencies/db.py``: ``except Exception: rollback;
        raise``).  The service must NOT swallow the error or commit.
        """
        mock_repo.find_user_by_email.return_value = None
        mock_org_repo.get_by_id.return_value = _make_org()
        mock_repo.get_by_uuid.return_value = _make_admin()
        mock_repo.create.return_value = _make_pending_user()
        mock_email_service.send_email.side_effect = ExternalServiceError(
            "SMTP server unreachable"
        )

        with (
            patch(
                "services.invite_service.secrets.token_urlsafe",
                return_value="raw-token",
            ),
            pytest.raises(ExternalServiceError) as exc,
        ):
            await service.invite_user(
                admin_user_id=ADMIN_ID,
                org_id=ORG_ID,
                payload=_invite_request(),
            )

        assert "SMTP" in str(exc.value)
        # Row was created, then the send failed — no commit anywhere in
        # between (the transaction stays open and is rolled back).
        mock_repo.create.assert_awaited_once()
        mock_email_service.send_email.assert_awaited_once()
        # Regression guard: a commit-then-send refactor (which would orphan
        # the pending row on email failure) must fail this test.
        mock_repo.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invite_email_service_none_raises(
        self,
        mock_repo: AsyncMock,
        mock_auth_service: AsyncMock,
        mock_org_repo: AsyncMock,
    ) -> None:
        """No email service → loud ExternalServiceError, never a silent invite."""
        service = InviteService(
            repo=mock_repo,
            auth_service=mock_auth_service,
            email_service=None,
            org_repo=mock_org_repo,
        )
        mock_repo.find_user_by_email.return_value = None
        mock_org_repo.get_by_id.return_value = _make_org()
        mock_repo.get_by_uuid.return_value = _make_admin()
        mock_repo.create.return_value = _make_pending_user()

        with pytest.raises(
            ExternalServiceError, match="Email service is not configured",
        ):
            await service.invite_user(
                admin_user_id=ADMIN_ID,
                org_id=ORG_ID,
                payload=_invite_request(),
            )

    # ── revoke_invite ───────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_revoke_invite_success(
        self,
        service: InviteService,
        mock_repo: AsyncMock,
    ) -> None:
        """Revoke hard-deletes the pending row in the org scope."""
        mock_repo.hard_delete_pending_user.return_value = 1

        await service.revoke_invite(org_id=ORG_ID, user_id=INVITEE_ID)

        mock_repo.hard_delete_pending_user.assert_awaited_once_with(
            organization_id=ORG_ID,
            user_id=INVITEE_ID,
        )

    @pytest.mark.asyncio
    async def test_revoke_invite_no_pending_row_404(
        self,
        service: InviteService,
        mock_repo: AsyncMock,
    ) -> None:
        """No pending invite in this org → 404 (rowcount 0)."""
        mock_repo.hard_delete_pending_user.return_value = 0

        with pytest.raises(NotFoundError) as exc:
            await service.revoke_invite(org_id=ORG_ID, user_id=OTHER_USER_ID)

        assert str(exc.value) == "No pending invite for this user."

    # ── get_invite_info ─────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_get_invite_info_happy_path(
        self,
        service: InviteService,
        mock_repo: AsyncMock,
        mock_org_repo: AsyncMock,
    ) -> None:
        """Valid unexpired token → org name + invitee identity."""
        mock_repo.find_user_by_invite_token.return_value = _make_pending_user()
        mock_org_repo.get_by_id.return_value = _make_org()

        result = await service.get_invite_info("raw-token")

        assert result.org_name == "Acme Corp"
        assert result.email == "alice@acme.com"
        assert result.name == "Alice Johnson"

    @pytest.mark.asyncio
    async def test_get_invite_info_unknown_token_generic_404(
        self,
        service: InviteService,
        mock_repo: AsyncMock,
    ) -> None:
        """Unknown token → generic 404 (never existed / used)."""
        mock_repo.find_user_by_invite_token.return_value = None

        with pytest.raises(NotFoundError) as exc:
            await service.get_invite_info("raw-token")

        assert str(exc.value) == "This invitation link is invalid or has expired."

    @pytest.mark.asyncio
    async def test_get_invite_info_expired_self_cleans_then_404(
        self,
        service: InviteService,
        mock_repo: AsyncMock,
    ) -> None:
        """Expired-but-present row → hard-deleted (self-clean), then 404."""
        stale = _make_pending_user(
            created_at=datetime.now(UTC) - timedelta(hours=INVITE_EXPIRY_HOURS + 1),
        )
        mock_repo.find_user_by_invite_token.return_value = stale

        with pytest.raises(NotFoundError) as exc:
            await service.get_invite_info("raw-token")

        assert str(exc.value) == "This invitation link is invalid or has expired."
        mock_repo.hard_delete_pending_user.assert_awaited_once_with(
            organization_id=ORG_ID,
            user_id=INVITEE_ID,
        )
        # The delete must be persisted BEFORE the 404 raise — the request-
        # scoped session dependency rolls back on the raised exception, so
        # the self-clean commit is what makes the cleanup stick.
        mock_repo.commit.assert_awaited_once()

    # ── accept_invite ───────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_accept_happy_path_claims_and_issues_tokens(
        self,
        service: InviteService,
        mock_repo: AsyncMock,
        mock_auth_service: AsyncMock,
    ) -> None:
        """Claim wins → password hashed, tokens issued from the RETURNING row."""
        claimed = SimpleNamespace(
            id=INVITEE_ID,
            organization_id=ORG_ID,
            role="member",
        )
        mock_repo.claim_invite.return_value = claimed

        with patch("services.invite_service.hash_password", return_value="hashed"):
            result = await service.accept_invite(
                token="raw-token",
                password="SecurePass1",
            )

        assert result == FAKE_TOKEN_RESPONSE
        mock_repo.claim_invite.assert_awaited_once()
        claim_kwargs = mock_repo.claim_invite.call_args.kwargs
        assert claim_kwargs["token_hash"] == (
            "34d328009b123fbbb0dc93f18b3e6de1ecf7b1a5783c33dff7ffe1926f09e943"
        )
        assert claim_kwargs["password_hash"] == "hashed"
        # Cutoff must be derived from INVITE_EXPIRY_HOURS as naive UTC —
        # otherwise the claim SQL and the service check can drift.
        expected_cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            hours=INVITE_EXPIRY_HOURS
        )
        assert claim_kwargs["cutoff"].tzinfo is None
        assert abs((expected_cutoff - claim_kwargs["cutoff"]).total_seconds()) < 5
        # Tokens issued from the atomic claim's RETURNING values — no
        # select-then-update, no stale identity-mapped row.
        mock_auth_service.issue_tokens.assert_awaited_once_with(
            user_id=INVITEE_ID,
            organization_id=ORG_ID,
            role="member",
        )

    @pytest.mark.asyncio
    async def test_accept_weak_password_raises_before_claim(
        self,
        service: InviteService,
        mock_repo: AsyncMock,
    ) -> None:
        """Weak password → ValidationError, claim never attempted."""
        from core.exceptions import ValidationError

        with pytest.raises(ValidationError):
            await service.accept_invite(token="raw-token", password="short")

        mock_repo.claim_invite.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_accept_race_loser_generic_404(
        self,
        service: InviteService,
        mock_repo: AsyncMock,
    ) -> None:
        """Claim matches zero rows and no stale row → generic 404 (race loser)."""
        mock_repo.claim_invite.return_value = None
        mock_repo.find_user_by_invite_token.return_value = None

        with (
            patch("services.invite_service.hash_password", return_value="hashed"),
            pytest.raises(NotFoundError) as exc,
        ):
            await service.accept_invite(token="raw-token", password="SecurePass1")

        assert str(exc.value) == "This invitation link is invalid or has expired."
        mock_repo.hard_delete_pending_user.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_accept_expired_self_cleans_then_404(
        self,
        service: InviteService,
        mock_repo: AsyncMock,
    ) -> None:
        """Claim misses on an expired-but-present row → self-clean, then 404."""
        stale = _make_pending_user(
            created_at=datetime.now(UTC) - timedelta(hours=INVITE_EXPIRY_HOURS + 1),
        )
        mock_repo.claim_invite.return_value = None
        mock_repo.find_user_by_invite_token.return_value = stale

        with (
            patch("services.invite_service.hash_password", return_value="hashed"),
            pytest.raises(NotFoundError) as exc,
        ):
            await service.accept_invite(token="raw-token", password="SecurePass1")

        assert str(exc.value) == "This invitation link is invalid or has expired."
        mock_repo.hard_delete_pending_user.assert_awaited_once_with(
            organization_id=ORG_ID,
            user_id=INVITEE_ID,
        )
        # Same persist-before-raise contract as the info path.
        mock_repo.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_accept_replay_after_first_accept_404(
        self,
        service: InviteService,
        mock_repo: AsyncMock,
    ) -> None:
        """Second accept of the same token → generic 404 (row already claimed)."""
        # First accept wins.
        mock_repo.claim_invite.return_value = SimpleNamespace(
            id=INVITEE_ID,
            organization_id=ORG_ID,
            role="member",
        )
        with patch("services.invite_service.hash_password", return_value="hashed"):
            await service.accept_invite(token="raw-token", password="SecurePass1")

        # Replay: the row's hash is now NULL, so the claim misses and the
        # row is gone from find_user_by_invite_token too (NULL hash).
        mock_repo.claim_invite.return_value = None
        mock_repo.find_user_by_invite_token.return_value = None

        with (
            patch("services.invite_service.hash_password", return_value="hashed"),
            pytest.raises(NotFoundError) as exc,
        ):
            await service.accept_invite(token="raw-token", password="SecurePass1")

        assert str(exc.value) == "This invitation link is invalid or has expired."
