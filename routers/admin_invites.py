"""Admin endpoints for the invite-by-email flow.

An org admin invites a user by email; the invitee sets a password via the
emailed magic link (``POST /v1/auth/invites/accept``).  Both endpoints
require the org ``admin`` role (JWT only) — the router is a thin adapter:
it extracts the org ID and admin user from the admin-gate dependencies,
delegates to :class:`InviteService`, and maps the result to a schema.

The raw invite token never appears in any response — it is delivered to
the invitee by email only.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Response

from core.audit import audit_action
from dependencies.auth import get_dashboard_user, require_org_admin
from dependencies.services import get_invite_service
from schemas.auth import InviteRequest, InviteResponse
from services.invite_service import InviteService  # noqa: TC001

router = APIRouter(
    prefix="/v1/admin/users",
    tags=["Admin - Users"],
)


@router.post(
    "/invite",
    response_model=InviteResponse,
    status_code=201,
    summary="Invite a user by email",
    description=(
        "Creates a pending member user in the authenticated organization "
        "and emails them a magic link to set a password.  The response "
        "never contains the raw token.  Requires the org admin role (JWT)."
    ),
)
@audit_action("admin.user_invite", "user", "Admin invited user")
async def invite_user(
    payload: InviteRequest,
    org_id: str = Depends(require_org_admin),  # noqa: B008
    user_id: str = Depends(get_dashboard_user),  # noqa: B008
    service: InviteService = Depends(get_invite_service),  # noqa: B008
) -> InviteResponse:
    """Invite a user by email (admin-gated).

    Args:
        payload: Invitee email + name.
        service: Invite service (injected).
        org_id: Authenticated organization ID (admin-gated).
        user_id: Authenticated admin user ID (for inviter attribution).

    Returns:
        The pending user's id/email/name.

    Raises:
        ConflictError: If an account with this email already exists (→ 409).
        ExternalServiceError: If the invite email cannot be sent (→ 502).
    """
    return await service.invite_user(
        admin_user_id=UUID(user_id),
        org_id=UUID(org_id),
        payload=payload,
    )


@router.delete(
    "/invites/{user_id}",
    status_code=204,
    summary="Revoke a pending invite",
    description=(
        "Hard-deletes the pending user row for an invite that has not been "
        "accepted.  Only pending invites are affected — accepted or "
        "ordinary users are never touched.  Requires the org admin role (JWT)."
    ),
)
@audit_action("admin.user_invite_revoke", "user", "Admin revoked user invite")
async def revoke_invite(
    user_id: UUID,
    org_id: str = Depends(require_org_admin),  # noqa: B008
    service: InviteService = Depends(get_invite_service),  # noqa: B008
) -> Response:
    """Revoke a pending invite (admin-gated).

    Returns an explicit empty ``Response(status_code=204)`` — returning
    bare ``None`` would make FastAPI serialize ``null`` as a JSON body,
    which violates the 204 no-body contract and breaks Uvicorn's
    httptools send.

    Args:
        user_id: The pending user's UUID (path param).
        service: Invite service (injected).
        org_id: Authenticated organization ID (admin-gated).

    Raises:
        NotFoundError: If there is no pending invite for this user in this
            org (→ 404).
    """
    await service.revoke_invite(org_id=UUID(org_id), user_id=user_id)
    return Response(status_code=204)
