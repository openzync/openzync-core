"""Jinja2 prompt template rendering for ARQ worker tasks.

All prompt templates live in the ``prompt_templates`` database table and are
resolved at runtime via :func:`resolve_prompt_template`.  The resolved
``template_text`` is then passed to :func:`render_prompt`.

The ``.jinja2`` files under ``prompts/`` exist solely as seed sources for
the database migration — they are **never** loaded at runtime.

Usage:
    from services.worker.prompt_renderer import (
        render_prompt,
        resolve_prompt_template,
    )

    template_text = await resolve_prompt_template(
        "extract_facts_v1", org_id="...", db_session_factory=...
    )
    if template_text:
        prompt = render_prompt(
            "extract_facts_v1",
            template_text=template_text,
            conversation=user_message,
        )
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from jinja2 import Environment

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_ENV = Environment(
    # LLM prompts are intentionally unescaped — we pass trusted template
    # variables, not user-controlled HTML.
    autoescape=False,  # noqa: S701 — LLM prompts are trusted templates, not user-controlled HTML.
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_prompt(
    template_name: str,
    *,
    template_text: str | None = None,
    **kwargs,
) -> str:
    """Render a Jinja2 prompt template with the given context variables.

    ``template_text`` **must** be provided — this function never falls back
    to the filesystem.  Use :func:`resolve_prompt_template` to obtain the
    template text from the database first.

    Args:
        template_name: Logical template name (e.g. ``"extract_facts_v1"``).
            Used for error messages and traceability only when
            ``template_text`` is ``None``.
        template_text: Raw template string to render.  **Required** —
            raises ``ValueError`` if ``None``.
        **kwargs: Template variables passed to the render context.  Keys must
            match the variable names used in the template.

    Returns:
        The fully rendered prompt string, ready to send to the LLM.

    Raises:
        ValueError: If ``template_text`` is ``None`` (the caller must resolve
            the template from the database first).

    Example:
        .. code-block:: python

            prompt = render_prompt(
                "extract_facts_v1",
                template_text="Extract facts from: {{ conversation }}",
                conversation="My email is alice@acme.com",
            )
    """
    if template_text is None:
        raise ValueError(
            f"No template_text provided for '{template_name}' — "
            f"prompt must be resolved from the database first via "
            f"resolve_prompt_template()"
        )

    template = _ENV.from_string(template_text)
    return template.render(**kwargs)


async def resolve_prompt_template(
    template_name: str,
    org_id: UUID | str,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> str | None:
    """Resolve a prompt template from the DB, falling back to system default.

    Looks up an active prompt template for the given organisation first.
    If no org-specific override exists the system default
    (``organization_id IS NULL``) is returned.  Returns ``None`` when neither
    exists — the caller may use the snippet directly.

    The :class:`~repositories.prompt_template_repository.PromptTemplateRepository`
    import is deferred to avoid circular dependencies since the renderer is
    imported early by worker bootstrap code.

    Args:
        template_name: Logical template name (e.g. ``"extract_facts_v1"``).
        org_id: Organisation UUID (string or ``UUID`` instance) used for
            scope resolution.
        db_session_factory: An ``async_sessionmaker`` bound to the write
            database engine, used to create a session for the query.

    Returns:
        The active template body (``template_text``), or ``None`` if no
        active template exists in either the org or system scope.

    Example:
        .. code-block:: python

            template_text = await resolve_prompt_template(
                "extract_facts_v1",
                org_id="3f3e6b8a-1c2d-4e5f-9a0b-1c2d3e4f5a6b",
                db_session_factory=async_session_factory,
            )
            if template_text is not None:
                prompt = render_prompt(
                    template_name,
                    template_text=template_text,
                    **context,
                )
    """
    if isinstance(org_id, str):
        org_id = UUID(org_id)

    # Lazy import to avoid circular dependencies — PromptTemplateRepository
    # imports models which may pull in core/db.py before the engine is ready.
    from repositories.prompt_template_repository import PromptTemplateRepository  # noqa: PLC0415, I001 — lazy import; must remain inline.

    async with db_session_factory() as session:
        repo = PromptTemplateRepository(session)
        template = await repo.get_active(org_id=org_id, template_name=template_name)

    # TechLead note: get_active() already falls back to the system default
    # (organization_id IS NULL) internally when no org-specific override
    # exists, so a single call suffices here.
    return template.template_text if template is not None else None
