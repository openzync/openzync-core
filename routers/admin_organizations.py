"""Admin router for organization-level configuration management.

Provides CRUD for prompt templates and custom instructions,
scoped to the authenticated user's organization.
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

    Returns the org-specific override if one exists, otherwise the system
    default.  Raises 404 when no template exists at either level.
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
    """List all versions of a named template visible to this org.

    Includes both system default versions and org-specific versions,
    ordered by version descending (newest first).  Raises 404 if the
    template name does not exist in either scope.
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

    If no system default exists for this template name, a system-level
    default is created first from the provided text so other organizations
    have a fallback to inherit.  The org-specific version is then created
    as a higher-version override.

    Invalidates any Redis cache entries for this template after update.
    """
    repo = PromptTemplateRepository(db)

    # ── Ensure a system default exists for this name ────────────────────
    # TechLead note: This inline model usage is a pragmatic short-cut until
    # PromptTemplateRepository gains a create_system_default() method.
    system_default = await repo.get_system_default(name)
    if system_default is None:
        from models.prompt_template import PromptTemplate

        system_template = PromptTemplate(
            organization_id=None,
            template_name=name,
            template_text=body.template_text,
            version=1,
            description=body.description,
            is_active=True,
        )
        db.add(system_template)
        await db.flush()
        await db.refresh(system_template)

    # ── Create the org-specific override ────────────────────────────────
    template = await repo.set_for_org(
        org_id=uuid.UUID(org_id),
        name=name,
        text=body.template_text,
        desc=body.description,
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
    org-specific versions are deactivated.  Raises 404 if the target
    version does not exist in either the org or system scope.
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

    After deletion the organization falls back to the system default for
    this template.  Raises 404 if no org-specific override currently
    exists.
    """
    repo = PromptTemplateRepository(db)

    # Check that an org-specific override actually exists.
    # get_active() falls back to system default, so we verify the source.
    active = await repo.get_active(uuid.UUID(org_id), name)
    if active is None or active.organization_id != uuid.UUID(org_id):
        raise HTTPException(
            status_code=404,
            detail=(
                f"No org-specific override found for template '{name}'; "
                f"the organization is already using the system default."
            ),
        )

    await repo.delete_for_org(org_id=uuid.UUID(org_id), name=name)


@router.post(
    "/prompts/{name}/promote/{version}",
    response_model=PromptTemplateDetail,
    status_code=201,
)
async def promote_prompt_template(
    name: str,
    version: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_org_id),
    _user_id: str = Depends(get_dashboard_user),
) -> PromptTemplateDetail:
    """Promote a prompt template version to system default.

    The target version is looked up in the caller's org scope first,
    then the system scope.  After promotion, all organizations without
    a custom override will use this version.

    **This is a global action** — it affects every organization on the
    platform that has not created its own override for this template.
    """
    repo = PromptTemplateRepository(db)
    try:
        template = await repo.promote_to_system_default(
            caller_org_id=uuid.UUID(org_id),
            name=name,
            target_version=version,
        )
    except ValueError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err

    # Invalidate Redis cache for the system default so workers pick up
    # the change without waiting for TTL expiry.
    redis = getattr(request.app.state, "redis", None)
    if redis is not None:
        await redis.delete(f"prompt_template:default:{name}")

    return PromptTemplateDetail.model_validate(template)


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
