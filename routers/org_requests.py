"""In-app organization request endpoints — HTTP adapter layer only.

``POST /v1/org-requests`` lets any authenticated dashboard user request
a new organization.  The platform ``org_creation_policy`` (see
``core.system_config``) decides the outcome: instant creation under
``allow_all``, or a pending approval-queue entry under ``approvals``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from core.audit import audit_action
from dependencies.auth import get_dashboard_user
from dependencies.services import get_org_request_service
from schemas.org_requests import OrgRequestCreate, OrgRequestResponse
from services.org_request_service import OrgRequestService  # noqa: TC001

router = APIRouter(
    prefix="/v1/org-requests",
    tags=["Organization Requests"],
)


@router.post(
    "",
    response_model=OrgRequestResponse,
    status_code=201,
    summary="Request a new organization",
    description=(
        "Creates an organization on behalf of the caller.  Under the "
        "platform ``allow_all`` policy the org is created instantly and "
        "the designated admin receives a verification email.  Under "
        "``approvals`` (with in-app scope) the org enters a pending "
        "queue for superadmin approval.  Rejected policies return 403."
    ),
)
@audit_action("org_request.create", "organization", "Organization requested")
async def request_org_creation(
    payload: OrgRequestCreate,
    request: Request,
    service: OrgRequestService = Depends(get_org_request_service),  # noqa: B008
    _user_id: str = Depends(get_dashboard_user),  # noqa: B008
) -> OrgRequestResponse:
    """Create (or queue) a new organization.

    Args:
        payload: Organization name + designated admin email/name.
        request: Incoming HTTP request.
        service: Injected org-request service.
        _user_id: Authenticated dashboard user (any role may request).

    Returns:
        An ``OrgRequestResponse`` with the request status.

    Raises:
        AuthorizationError: 403 under ``reject_all`` or when the
            approvals policy excludes the in-app channel.
        ConflictError: 409 if the designated admin email is already taken.
    """
    return await service.request_org_creation(payload)
