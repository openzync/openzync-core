"""Admin router for custom instructions management.

Provides CRUD for extraction custom instructions in the singleton
platform org.

Prompt templates are disk-only (``services/worker/prompts/manifest.yaml``
+ ``.jinja2`` files, cached at startup) — the ``/prompts*`` API and the
``prompt_templates`` table were removed.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from core.audit import audit_action
from dependencies.auth import require_permission
from dependencies.db import get_db
from schemas.custom_instructions import (
    CustomInstructionSchema,
    CustomInstructionsResponse,
    SetCustomInstructionsRequest,
)

# ── Router ─────────────────────────────────────────────────────────────────

router = APIRouter(
    prefix="/admin/custom-instructions",
    tags=["Admin - Custom Instructions"],
)


async def get_custom_instruction_service(
    db: AsyncSession = Depends(get_db),
):
    """Dependency that yields an initialised CustomInstructionService."""
    from services.custom_instruction_service import CustomInstructionService

    return CustomInstructionService(db)


# ═══════════════════════════════════════════════════════════════════════════════
# Custom Instructions
# ═══════════════════════════════════════════════════════════════════════════════


@router.get(
    "",
    response_model=CustomInstructionsResponse,
)
async def list_custom_instructions(
    service=Depends(get_custom_instruction_service),
    user_id: str = Depends(require_permission("configuration:read")),
) -> CustomInstructionsResponse:
    """List all extraction custom instructions for the platform."""
    instructions = await service.list_for_scope(
        user_id=uuid.UUID(user_id),
        scope="extraction",
    )
    return CustomInstructionsResponse(
        data=[CustomInstructionSchema(name=i.name, text=i.text) for i in instructions],
    )


@router.put(
    "",
    response_model=CustomInstructionsResponse,
    status_code=201,
)
@audit_action("instruction.update", "instruction", "Instructions updated")
async def set_custom_instructions(
    body: SetCustomInstructionsRequest,
    service=Depends(get_custom_instruction_service),
    user_id: str = Depends(require_permission("configuration:write")),
) -> CustomInstructionsResponse:
    """Replace all extraction custom instructions for the platform.

    The existing instructions in the ``extraction`` scope are deleted and
    replaced atomically with the provided list.
    """
    instructions_data = [i.model_dump() for i in body.instructions]
    instructions = await service.set_for_scope(
        user_id=uuid.UUID(user_id),
        scope="extraction",
        target_id=None,
        instructions=instructions_data,
    )
    return CustomInstructionsResponse(
        data=[CustomInstructionSchema(name=i.name, text=i.text) for i in instructions],
    )


@router.delete(
    "",
    status_code=204,
)
@audit_action("instruction.delete", "instruction", "Instructions deleted")
async def clear_custom_instructions(
    service=Depends(get_custom_instruction_service),
    user_id: str = Depends(require_permission("configuration:write")),
) -> Response:
    """Clear all extraction custom instructions for the platform.

    All instructions in the ``extraction`` scope are removed.  This is a
    no-op if no instructions exist.
    """
    await service.clear_for_scope(
        user_id=uuid.UUID(user_id),
        scope="extraction",
    )
    return Response(status_code=204)
