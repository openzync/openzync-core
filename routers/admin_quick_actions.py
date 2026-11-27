"""Admin dashboard quick-actions endpoint — context-aware action suggestions.

Returns a list of actionable items for the platform-level overview page,
based on the platform user's current state (projects, config, users, etc.).

All endpoints require JWT authentication (dashboard session).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from dependencies.auth import require_user
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
    user_id: str = Depends(require_user),
) -> QuickActionsResponse:
    """Return a prioritized list of quick actions based on platform state.

    Args:
        service: Quick actions service (injected).
        user_id: Authenticated dashboard user ID (ensures JWT auth).

    Returns:
        QuickActionsResponse with ordered list of actions.
    """
    actions = await service.get_actions(UUID(user_id))
    return QuickActionsResponse(
        actions=[
            QuickActionItem(
                label=a.label, href=a.href, icon=a.icon,
                description=a.description,
            )
            for a in actions
        ]
    )
