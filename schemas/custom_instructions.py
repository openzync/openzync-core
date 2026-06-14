"""Pydantic schemas for organization-level custom instructions.

Custom instructions are named domain-specific text snippets that guide
extraction behavior.  They are scoped by (organization_id, scope, target_id)
where scope is one of ``extraction`` or ``user_summary``.

Each instruction has a human-readable ``name`` (e.g. "legal_domain") and
the ``text`` content that is injected into the extraction prompt.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CustomInstructionSchema(BaseModel):
    """A single named custom instruction.

    Attributes:
        name: Human-readable label (e.g. "legal_domain", "healthcare").
        text: The instruction text injected into the extraction prompt.
    """

    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Human-readable instruction label.",
        examples=["legal_domain"],
    )
    text: str = Field(
        ...,
        min_length=1,
        description="The instruction text content.",
        examples=[
            "This application operates in the legal domain. "
            "Common terminology includes: consideration, estoppel, "
            "tort, indemnification, force majeure..."
        ],
    )


class CustomInstructionsResponse(BaseModel):
    """Response wrapper for listing custom instructions."""

    data: list[CustomInstructionSchema]


class SetCustomInstructionsRequest(BaseModel):
    """Request to replace all custom instructions for a scope.

    The existing instructions for the scope are replaced atomically.
    """

    instructions: list[CustomInstructionSchema] = Field(
        ...,
        max_length=50,
        description="List of named instruction pairs to set.",
    )
