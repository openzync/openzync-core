"""Pydantic models representing raw LLM output contracts.

These models define the **expected output shape** that the LLM must produce
for each extraction task.  They are distinct from the API response schemas
(in the same package) which include DB-generated fields (``id``,
``created_at``, …) — these models contain **only** the fields the LLM is
asked to emit.

Every model is designed to be passed as ``response_model`` to
:meth:`core.llm.LLMBackend.chat`, which will:
* auto-inject the model's JSON schema into the system prompt,
* validate the response against the model,
* retry with error context on failure.

Usage::

    from schemas.llm_outputs import ClassificationOutput

    response = await backend.chat(
        messages,
        response_model=ClassificationOutput,
        temperature=0.0,
    )
    # response.content is guaranteed valid JSON matching ClassificationOutput
    data = ClassificationOutput.model_validate_json(response.content)
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — pydantic resolves field annotations at runtime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ═══════════════════════════════════════════════════════════════════════════════
# Entity extraction
# ═══════════════════════════════════════════════════════════════════════════════


class EntityOutput(BaseModel):
    """A single entity extracted from a conversation turn."""

    name: str
    type: str
    summary: str | None = None


class RelationshipOutput(BaseModel):
    """A directed relationship between two entities.

    Field names match the ``subject/predicate/object`` convention used in
    all extraction prompt templates (not ``source/target/relation``).
    """

    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    object: str = Field(min_length=1, alias="object")

    @field_validator("subject", "predicate", "object")
    @classmethod
    def must_not_be_empty(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("Field must not be empty or whitespace-only")
        return stripped


class EntityExtractionOutput(BaseModel):
    """Expected LLM output for entity extraction.

    The LLM must emit a JSON object with ``entities`` and ``relationships``
    arrays (both may be empty).
    """

    entities: list[EntityOutput] = []
    relationships: list[RelationshipOutput] = []


# ═══════════════════════════════════════════════════════════════════════════════
# Fact extraction
# ═══════════════════════════════════════════════════════════════════════════════


class FactOutput(BaseModel):
    """A single subject-predicate-object triple."""

    subject: str
    predicate: str
    object: str = Field(alias="object")
    confidence: float = 0.0
    subject_type: str | None = None
    object_type: str | None = None
    valid_from: datetime | None = None
    """Explicit temporal validity start (ISO-8601) — only when the fact
    text itself states an explicit validity window/event date."""

    valid_to: datetime | None = None
    """Explicit temporal validity end (ISO-8601) — only when the fact
    text itself states an explicit validity window/event date."""


class FactExtractionOutput(BaseModel):
    """Expected LLM output for fact extraction.

    The LLM must emit a JSON object with a ``facts`` array.  Wrapping the
    array in an object ensures consistent Pydantic validation and avoids
    ambiguous top-level arrays.
    """

    facts: list[FactOutput] = []


# ═══════════════════════════════════════════════════════════════════════════════
# Dialog classification
# ═══════════════════════════════════════════════════════════════════════════════


class ClassificationOutput(BaseModel):
    """Expected LLM output for dialog classification.

    All fields are optional — the LLM may choose not to classify a dimension
    if the input is ambiguous.  The worker applies further label validation
    against the org's configured allowed sets.
    """

    intent: str | None = None
    emotion: str | None = None
    valence: str | None = None
    arousal: str | None = None
    confidence: float = 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Structured extraction
# ═══════════════════════════════════════════════════════════════════════════════


class StructuredExtractionOutput(BaseModel):
    """Expected LLM output for structured data extraction.

    Accepts **any** JSON object keys — the shape is defined by the org's
    configured extraction schemas, which vary per deployment.  This model
    ensures the LLM returned a valid JSON object; further schema-level
    validation is performed by the worker via ``_validate_against_schema``.
    """

    model_config = ConfigDict(extra="allow")


# ═══════════════════════════════════════════════════════════════════════════════
# Combined enrichment output
# ═══════════════════════════════════════════════════════════════════════════════


class InvalidationOutput(BaseModel):
    """An LLM-detected contradiction requiring an existing fact to close.

    Emitted only when the current message explicitly contradicts or updates
    a fact from the prompt's ``EXISTING FACTS`` table — never for stylistic
    differences, summarization, or same-meaning rephrasing without a change.
    The cited fact is closed by :class:`FactInvalidationService` inside the
    enrichment transaction; the optional successor takes over its range.
    """

    existing_fact_ref: str
    """``"E{index}"`` — 1-based reference into the prompt's EXISTING FACTS
    table pointing at the fact this message contradicts."""

    action: Literal["invalidate"]
    """The only supported action — close the referenced fact."""

    reason: str
    """One sentence explaining the contradiction or update."""

    successor_fact_ref: str | None = None
    """``"N{index}"`` — 1-based reference into this response's ``facts``
    array for the fact carrying the new value, or ``None`` when the message
    only retracts/negates (no replacement value given)."""


class CombinedLLMOutput(BaseModel):
    """Single LLM response combining all episode enrichment tasks.

    Used by the ``enrich_episode`` worker to produce all enrichment outputs
    in a single LLM call instead of 4 separate calls.  Each field maps to
    one of the previously independent LLM responses.
    """

    classification: ClassificationOutput = Field(default_factory=ClassificationOutput)
    """Classification result (intent, emotion, valence, arousal)."""

    entities: list[EntityOutput] = Field(default_factory=list)
    """Named entities extracted from the episode."""

    relationships: list[RelationshipOutput] = Field(default_factory=list)
    """Directed subject-predicate-object triples between entities."""

    facts: list[FactOutput] = Field(default_factory=list)
    """Factual knowledge triples with confidence scores."""

    structured_extractions: dict[str, Any] = Field(default_factory=dict)
    """Org-defined structured extractions keyed by schema name."""

    invalidations: list[InvalidationOutput] = Field(default_factory=list)
    """Existing facts this message contradicts, each optionally replaced by
    a successor fact from the ``facts`` array.  Empty by default — keeps
    the schema stable for older model outputs that predate invalidation."""
