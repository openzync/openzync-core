"""Admin dashboard quick-actions endpoint — context-aware action suggestions.

Returns a list of actionable items for the org-level overview page,
based on the organization's current state (projects, config, users, etc.).

All endpoints require JWT authentication (dashboard session).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from dependencies.auth import get_dashboard_user, require_org_id
from dependencies.services import get_quick_actions_service
from services.quick_actions_service import QuickActionsService

router = APIRouter(
    prefix="/v1/admin/quick-actions",
    tags=["Admin - Quick Actions"],
)


class QuickActionItem(BaseModel):
    label: str
    href: str
    icon: str
    description: str | None = None


class QuickActionsResponse(BaseModel):
    actions: list[QuickActionItem]


@router.get(
    "",
    response_model=QuickActionsResponse,
    summary="Context-aware quick actions for the dashboard overview",
)
async def list_quick_actions(
    service: QuickActionsService = Depends(get_quick_actions_service),
    org_id: str = Depends(require_org_id),
    _user_id: str = Depends(get_dashboard_user),
) -> QuickActionsResponse:
    """Return a prioritized list of quick actions based on org state.

    Args:
        service: Quick actions service (injected).
        org_id: Authenticated organization ID (from JWT).
        _user_id: Authenticated dashboard user ID (ensures JWT auth).

    Returns:
        QuickActionsResponse with ordered list of actions.
    """
    actions = await service.get_actions(UUID(org_id))
    return QuickActionsResponse(actions=[QuickActionItem(**a) for a in actions])
