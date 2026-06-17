"""Admin router for organization-level configuration management.

Provides CRUD for prompt templates and custom instructions,
scoped to the authenticated user's organization.

After Option A:
- No more system-level prompt rows (``organization_id IS NULL``).
- Defaults are seeded from ``manifest.yaml`` + ``.jinja2`` files on disk.
- The ``promote`` endpoint has been removed — defaults change via git.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.auth import get_dashboard_user, require_org_id
from dependencies.db import get_db
from repositories.prompt_template_repository import PromptTemplateRepository
from schemas.custom_instructions import (
    CustomInstructionSchema,
    CustomInstructionsResponse,
    SetCustomInstructionsRequest,
)
from schemas.prompt_templates import (
    ImportPromptRequest,
    PromptTemplateDetail,
    PromptTemplateListResponse,
    PromptTemplateSummary,
    PromptTemplateVersionsResponse,
    SetPromptTemplateRequest,
    SystemPromptGroupsResponse,
)

# ── Router ─────────────────────────────────────────────────────────────────

router = APIRouter(
    prefix="/admin/org",
    tags=["Admin - Organizations"],
)


# ═══════════════════════════════════════════════════════════════════════════════
# Prompt Templates
# ═══════════════════════════════════════════════════════════════════════════════


@router.get(
    "/prompts",
    response_model=PromptTemplateListResponse,
)
async def list_prompt_templates(
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_org_id),
    _user_id: str = Depends(get_dashboard_user),
) -> PromptTemplateListResponse:
    """List all prompt template names with override status.

    Returns one entry per template name — includes the current version,
    whether the org has customised it, and its last-updated timestamp.
    """
    repo = PromptTemplateRepository(db)
    templates = await repo.list_names(uuid.UUID(org_id))
    return PromptTemplateListResponse(
        data=[PromptTemplateSummary(**t) for t in templates],
    )


@router.get(
    "/prompts/system",
    response_model=SystemPromptGroupsResponse,
)
async def list_system_prompts(
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_org_id),
    _user_id: str = Depends(get_dashboard_user),
) -> SystemPromptGroupsResponse:
    """List all system-default prompt templates grouped by base name.

    Returns every system-default version (not just active ones) so users
    can see old versions.  Each group is annotated with which template
    names the organisation has already imported.
    """
    repo = PromptTemplateRepository(db)
    groups = await repo.list_system_grouped(org_id=uuid.UUID(org_id))
    return SystemPromptGroupsResponse(groups=groups)


@router.post(
    "/prompts/import",
    response_model=PromptTemplateDetail,
    status_code=201,
)
async def import_system_prompt(
    body: ImportPromptRequest,
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_org_id),
    _user_id: str = Depends(get_dashboard_user),
) -> PromptTemplateDetail:
    """Import a system-default prompt template into the organisation.

    Creates an org-specific copy at ``version = 1`` with the text from
    the active system default.  Raises 409 if the template is already
    imported, or 404 if no active system default exists.
    """
    repo = PromptTemplateRepository(db)
    try:
        template = await repo.import_system_template(
            org_id=uuid.UUID(org_id),
            template_name=body.template_name,
        )
    except ValueError as err:
        msg = str(err)
        if "already imported" in msg:
            raise HTTPException(status_code=409, detail=msg) from err
        raise HTTPException(status_code=404, detail=msg) from err

    return PromptTemplateDetail.model_validate(template)


@router.post(
    "/prompts/{name}/set-default",
    response_model=PromptTemplateDetail,
)
async def set_prompt_type_default(
    name: str,
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_org_id),
    _user_id: str = Depends(get_dashboard_user),
) -> PromptTemplateDetail:
    """Mark a prompt template as the active default for its type.

    Sets ``is_default_for_type = True`` for this template and
    ``is_default_for_type = False`` for all other templates of the same
    type and scope.  Raises 404 if the template does not exist or has
    no ``type`` assigned.
    """
    repo = PromptTemplateRepository(db)
    try:
        template = await repo.set_as_type_default(
            org_id=uuid.UUID(org_id), name=name,
        )
    except ValueError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err

    return PromptTemplateDetail.model_validate(template)


@router.get(
    "/prompts/{name}",
    response_model=PromptTemplateDetail,
)
async def get_prompt_template(
    name: str,
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_org_id),
    _user_id: str = Depends(get_dashboard_user),
) -> PromptTemplateDetail:
    """Get the active template for an organization.

    Returns the org-specific template if it exists.  Raises 404 if not
    found (no system default fallback — defaults come from disk manifest).
    """
    repo = PromptTemplateRepository(db)
    template = await repo.get_active(uuid.UUID(org_id), name)
    if template is None:
        raise HTTPException(
            status_code=404,
            detail=f"Prompt template '{name}' not found",
        )
    return PromptTemplateDetail.model_validate(template)


@router.get(
    "/prompts/{name}/versions",
    response_model=PromptTemplateVersionsResponse,
)
async def list_prompt_template_versions(
    name: str,
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_org_id),
    _user_id: str = Depends(get_dashboard_user),
) -> PromptTemplateVersionsResponse:
    """List all versions of a named template for this org.

    Returns only org-scoped versions, ordered by version descending
    (newest first).  Raises 404 if no template exists with this name.
    """
    repo = PromptTemplateRepository(db)

    # Verify the template exists before listing versions.
    active = await repo.get_active(uuid.UUID(org_id), name)
    if active is None:
        raise HTTPException(
            status_code=404,
            detail=f"Prompt template '{name}' not found",
        )

    versions = await repo.list_versions(uuid.UUID(org_id), name)
    return PromptTemplateVersionsResponse(
        name=name,
        current_version=active.version,
        versions=[PromptTemplateDetail.model_validate(v) for v in versions],
    )


@router.put(
    "/prompts/{name}",
    response_model=PromptTemplateDetail,
    status_code=201,
)
async def set_prompt_template(
    name: str,
    body: SetPromptTemplateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_org_id),
    _user_id: str = Depends(get_dashboard_user),
) -> PromptTemplateDetail:
    """Create a new org-specific version of a prompt template.

    Creates an org-scoped copy at ``version = max(existing) + 1``.
    If no version exists yet for this template name, version starts at 1.

    Invalidates any Redis cache entries for this template after update.

    Note:
        System-level defaults no longer exist (Option A).  Defaults come
        from ``manifest.yaml`` on disk.  If you want to start from the
        disk default, import it first via ``POST /admin/org/prompts/import``
        and then edit the org-specific copy.
    """
    repo = PromptTemplateRepository(db)

    # ── Create the org-specific override ────────────────────────────────
    template = await repo.set_for_org(
        org_id=uuid.UUID(org_id),
        name=name,
        text=body.template_text,
        desc=body.description,
        template_type=body.type,
    )

    # ── Invalidate Redis cache for this template ────────────────────────
    redis = getattr(request.app.state, "redis", None)
    if redis is not None:
        await redis.delete(f"prompt_template:{org_id}:{name}")

    return PromptTemplateDetail.model_validate(template)


@router.post(
    "/prompts/{name}/rollback/{version}",
    response_model=PromptTemplateDetail,
)
async def rollback_prompt_template(
    name: str,
    version: int,
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_org_id),
    _user_id: str = Depends(get_dashboard_user),
) -> PromptTemplateDetail:
    """Rollback to a previous version of a prompt template.

    Creates a **new** version whose ``template_text`` is copied from the
    target version.  The new version is activated and all previously active
    versions are deactivated.  Raises 404 if the target version does not
    exist in the org scope.
    """
    repo = PromptTemplateRepository(db)
    try:
        template = await repo.rollback(
            org_id=uuid.UUID(org_id),
            name=name,
            version=version,
        )
    except ValueError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err

    return PromptTemplateDetail.model_validate(template)


@router.delete(
    "/prompts/{name}",
    status_code=204,
)
async def delete_prompt_template_override(
    name: str,
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_org_id),
    _user_id: str = Depends(get_dashboard_user),
) -> None:
    """Delete all org-specific versions of a prompt template.

    After deletion the organisation no longer has a copy of this
    template.  Re-import from the disk manifest via
    ``POST /admin/org/prompts/import`` if needed.  Raises 404 if no
    org-specific override currently exists.
    """
    repo = PromptTemplateRepository(db)

    # Check that an org-specific override actually exists.
    active = await repo.get_active(uuid.UUID(org_id), name)
    if active is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No template found with name '{name}' for this organisation."
            ),
        )

    if active.is_default_for_type:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot delete '{name}' because it is the active default "
                f"for its type. Set another template as the type default first."
            ),
        )

    await repo.delete_for_org(org_id=uuid.UUID(org_id), name=name)


# ── ``POST /prompts/{name}/promote/{version}`` removed (Option A) ──────────
#
# System-level prompt rows (organization_id IS NULL) no longer exist.
# The source of truth for defaults is services/worker/prompts/manifest.yaml
# plus the .jinja2 files on disk.  Defaults change via git, not via the API.
# If hot-promotion is needed later, implement a ``promoted_defaults`` table
# that overlays on the manifest at seeding time.


# ═══════════════════════════════════════════════════════════════════════════════
# Custom Instructions
# ═══════════════════════════════════════════════════════════════════════════════


@router.get(
    "/custom-instructions",
    response_model=CustomInstructionsResponse,
)
async def list_custom_instructions(
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_org_id),
    _user_id: str = Depends(get_dashboard_user),
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
async def set_custom_instructions(
    body: SetCustomInstructionsRequest,
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_org_id),
    _user_id: str = Depends(get_dashboard_user),
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
async def clear_custom_instructions(
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_org_id),
    _user_id: str = Depends(get_dashboard_user),
) -> None:
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
