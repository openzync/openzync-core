"""Admin router for organization-level configuration management.

Provides CRUD for custom instructions, scoped to the authenticated
user's organization.

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
    prefix="/admin/org",
    tags=["Admin - Organizations"],
)


# ═══════════════════════════════════════════════════════════════════════════════
# Custom Instructions
# ═══════════════════════════════════════════════════════════════════════════════


@router.get(
    "/custom-instructions",
    response_model=CustomInstructionsResponse,
)
async def list_custom_instructions(
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_permission("configuration:read")),
) -> CustomInstructionsResponse:
    """List all extraction custom instructions for the organization."""
    from repositories.custom_instruction_repository import (
        CustomInstructionRepository,
    )

    repo = CustomInstructionRepository(db)
    instructions = await repo.get_by_scope(
        org_id=uuid.UUID(org_id),
        scope="extraction",
    )
    return CustomInstructionsResponse(
        data=[CustomInstructionSchema(name=i.name, text=i.text) for i in instructions],
    )


@router.put(
    "/custom-instructions",
    response_model=CustomInstructionsResponse,
    status_code=201,
)
@audit_action("instruction.update", "instruction", "Instructions updated")
async def set_custom_instructions(
    body: SetCustomInstructionsRequest,
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_permission("configuration:write")),
) -> CustomInstructionsResponse:
    """Replace all extraction custom instructions for the organization.

    The existing instructions in the ``extraction`` scope are deleted and
    replaced atomically with the provided list.
    """
    from repositories.custom_instruction_repository import (
        CustomInstructionRepository,
    )

    instructions_data = [i.model_dump() for i in body.instructions]
    repo = CustomInstructionRepository(db)
    instructions = await repo.set_by_scope(
        org_id=uuid.UUID(org_id),
        scope="extraction",
        target_id=None,
        instructions=instructions_data,
    )
    return CustomInstructionsResponse(
        data=[CustomInstructionSchema(name=i.name, text=i.text) for i in instructions],
    )


@router.delete(
    "/custom-instructions",
    status_code=204,
)
@audit_action("instruction.delete", "instruction", "Instructions deleted")
async def clear_custom_instructions(
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_permission("configuration:write")),
) -> Response:
    """Clear all extraction custom instructions for the organization.

    All instructions in the ``extraction`` scope are removed.  This is a
    no-op if no instructions exist.
    """
    from repositories.custom_instruction_repository import (
        CustomInstructionRepository,
    )

    repo = CustomInstructionRepository(db)
    await repo.set_by_scope(
        org_id=uuid.UUID(org_id),
        scope="extraction",
        target_id=None,
        instructions=[],
    )
    return Response(status_code=204)
