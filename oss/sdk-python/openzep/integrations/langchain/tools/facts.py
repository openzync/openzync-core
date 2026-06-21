"""LangChain tools for OpenZep fact operations.

Provides tools that give LLM agents write access to the OpenZep facts
system — adding structured fact triples to a project's knowledge graph.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from openzep.client import AsyncOpenZep


class FactTripleInput(BaseModel):
    """A single fact triple."""

    subject: str = Field(..., description="Subject entity name.")
    predicate: str = Field(..., description="Relationship verb.")
    object: str = Field(..., description="Object entity name.")
    content: str | None = Field(
        default=None,
        description="Human-readable fact statement.",
    )
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Confidence score (0-1).",
    )


class AddFactsInput(BaseModel):
    """Input schema for adding facts."""

    project_id: str = Field(..., description="OpenZep project UUID.")
    facts: List[FactTripleInput] = Field(
        ..., min_length=1, max_length=500, description="Fact triples to add."
    )


class AddFactsTool(BaseTool):
    """Tool that adds structured fact triples to the OpenZep knowledge graph.

    Agents can use this to persist business data, user preferences,
    or relationship information extracted from conversations.
    """

    name: str = "add_facts"
    description: str = (
        "Add structured fact triples (subject-predicate-object) to the "
        "project's persistent knowledge graph. Use this to store business "
        "data, user preferences, or relationship information extracted "
        "from conversations."
    )
    args_schema: Type[BaseModel] = AddFactsInput
    client: AsyncOpenZep

    def _run(self, project_id: str, facts: list[dict[str, Any]]) -> str:
        """Add facts (sync)."""
        return _run_async(self._arun(project_id=project_id, facts=facts))

    async def _arun(self, project_id: str, facts: list[dict[str, Any]]) -> str:
        """Add facts (async)."""
        # Convert dicts to FactTriple-compatible dicts
        normalized: list[dict[str, Any]] = []
        for f in facts:
            normalized.append(
                {
                    "subject": f["subject"],
                    "predicate": f["predicate"],
                    "object": f["object"],
                    "content": f.get("content"),
                    "confidence": f.get("confidence", 1.0),
                }
            )

        result = await self.client.facts.add(project_id, normalized)
        return (
            f"Accepted {result.accepted_count} fact(s) "
            f"(job_id: {result.job_id})."
        )


def _run_async(coro: Any) -> Any:
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)
