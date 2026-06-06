"""Pydantic models for the facts (business data ingestion) domain."""

from __future__ import annotations

from pydantic import BaseModel, Field


class FactTriple(BaseModel):
    """A single fact triple for batch ingestion."""

    subject: str = Field(..., min_length=1, max_length=500, description="Subject entity name.")
    predicate: str = Field(..., min_length=1, max_length=200, description="Relationship verb.")
    object: str = Field(..., min_length=1, max_length=500, description="Object entity name.")
    content: str | None = Field(default=None, description="Human-readable fact statement.")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Confidence score.")


class FactBatchRequest(BaseModel):
    """Request body for ``POST /v1/users/{user_id}/facts``."""

    session_id: str | None = Field(default=None, description="Optional session ID.")
    facts: list[FactTriple] = Field(..., min_length=1, max_length=500)


class FactBatchResponse(BaseModel):
    """Response returned after successful fact batch ingestion."""

    job_id: str = Field(..., description="UUID of the async enrichment job.")
    accepted_count: int = Field(..., ge=0, description="Number of facts accepted.")
    status: str = Field(default="accepted")
    message: str = Field(default="Facts accepted for processing.")
