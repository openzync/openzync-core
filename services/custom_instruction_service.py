"""Service layer for custom instructions.

Custom instructions are named domain-specific text snippets that guide
LLM extraction behavior.  All business rules (e.g. duplicate-name
rejection) live in :class:`CustomInstructionService`; the repository stays
a thin delete+insert with a fail-loud unique-index backstop.
"""

from __future__ import annotations

from collections import Counter
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import ValidationError
from models.custom_instruction import CustomInstruction
from repositories.custom_instruction_repository import (
    CustomInstructionRepository,
)


def format_custom_instructions(instructions: list[dict]) -> str:
    """Format a list of ``{name, text}`` instruction dicts into a prompt-ready text block.

    Each instruction produces a Markdown section with the name as a level-3
    heading followed by the text body.  Sections are separated by a blank
    line, creating clear visual separation in the prompt.

    Args:
        instructions: List of dicts with ``name`` and ``text`` keys.

    Returns:
        A formatted string suitable for injection into an LLM prompt.
        Returns an empty string if ``instructions`` is empty.

    Examples:
        >>> format_custom_instructions([
        ...     {"name": "legal", "text": "Use legal terminology."},
        ... ])
        '### legal\\nUse legal terminology.'

        >>> format_custom_instructions([])
        ''
    """
    if not instructions:
        return ""

    blocks: list[str] = [
        f"### {instr['name']}\n{instr['text']}"
        for instr in instructions
    ]

    return "\n\n".join(blocks)


class CustomInstructionService:
    """Thin orchestration over ``CustomInstructionRepository``.

    Owns the duplicate-name rule; persistence (including the concurrent-
    write unique-index backstop) stays in the repository.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._repo = CustomInstructionRepository(db)

    async def list_for_scope(
        self,
        org_id: UUID,
        scope: str,
        target_id: UUID | None = None,
    ) -> list[CustomInstruction]:
        """List instructions for a scope + target, ordered by name."""
        return await self._repo.get_by_scope(
            org_id=org_id, scope=scope, target_id=target_id
        )

    async def set_for_scope(
        self,
        org_id: UUID,
        scope: str,
        target_id: UUID | None,
        instructions: list[dict],
    ) -> list[CustomInstruction]:
        """Replace all instructions for a scope + target.

        Raises:
            ValidationError: If ``instructions`` contains duplicate names.
        """
        counts = Counter(instr["name"] for instr in instructions)
        dupes = sorted(name for name, n in counts.items() if n > 1)
        if dupes:
            raise ValidationError(
                f"Duplicate custom instruction names: {', '.join(dupes)}",
                detail={"duplicates": dupes},
            )
        return await self._repo.set_by_scope(
            org_id=org_id,
            scope=scope,
            target_id=target_id,
            instructions=instructions,
        )

    async def clear_for_scope(
        self,
        org_id: UUID,
        scope: str,
        target_id: UUID | None = None,
    ) -> None:
        """Delete all instructions for a scope + target."""
        await self._repo.delete_by_scope(
            org_id=org_id, scope=scope, target_id=target_id
        )
