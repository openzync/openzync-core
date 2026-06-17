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

from pydantic import BaseModel, ConfigDict, Field


# ═══════════════════════════════════════════════════════════════════════════════
# Entity extraction
# ═══════════════════════════════════════════════════════════════════════════════


class EntityOutput(BaseModel):
    """A single entity extracted from a conversation turn."""

    name: str
    type: str
    summary: str | None = None


class RelationshipOutput(BaseModel):
    """A directed relationship between two entities."""

    source: str
    target: str
    relation: str


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
