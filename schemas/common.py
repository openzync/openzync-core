"""Common Pydantic schemas used across multiple domains.

Includes:
- ``PaginatedResponse[T]`` — generic wrapper for cursor / offset pagination.
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    """Generic paginated response wrapper.

    Use as the response model for list endpoints:

        class ItemResponse(BaseModel): ...

        @router.get("/items", response_model=PaginatedResponse[ItemResponse])
        async def list_items(...) -> PaginatedResponse[ItemResponse]: ...

    Attributes:
        data: The page of results.
        next_cursor: Opaque cursor for the next page (cursor-based pagination).
        has_more: ``True`` when there are additional pages beyond this one.
        total: Total number of matching records across all pages (optional).
    """

    data: list[T]
    next_cursor: str | None = None
    has_more: bool = False
    total: int | None = None
