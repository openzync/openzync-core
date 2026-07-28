"""Global search schemas — cross-resource search results."""

from __future__ import annotations

from pydantic import BaseModel


class GlobalSearchItem(BaseModel):
    """A single result from a global search."""

    type: str  # "project", "user", "session"
    id: str
    label: str  # primary display text (project name, user email, session external_id)
    subtitle: str | None  # secondary text (project description, user name, project name)
    href: str  # frontend URL for navigation


class GlobalSearchResponse(BaseModel):
    """Response from the global search endpoint."""

    results: list[GlobalSearchItem]
    query: str
