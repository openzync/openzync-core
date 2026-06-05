"""Common Pydantic schemas used across multiple domains.

Includes:
- ``PaginatedResponse[T]`` — generic wrapper for cursor / offset pagination.
- ``ErrorResponse`` — RFC 7807 Problem Details structure.
- ``ValidationErrorDetail`` — per-field validation error.
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


class ErrorResponse(BaseModel):
    """RFC 7807 Problem Details response body.

    See https://www.rfc-editor.org/rfc/rfc7807 for the specification.

    Attributes:
        type: A URI that identifies the problem type.
        title: A short, human-readable summary.
        status: The HTTP status code.
        detail: A human-readable explanation specific to this occurrence.
        instance: A URI that identifies the specific occurrence (optional).
        request_id: The correlation ID for this request (optional).
    """

    type: str
    title: str
    status: int
    detail: str
    instance: str | None = None
    request_id: str | None = None


class ValidationErrorDetail(BaseModel):
    """Per-field validation error detail.

    Mirrors the structure returned by FastAPI's default ``RequestValidationError``
    handler.

    Attributes:
        loc: Location path (e.g. ``["body", "email"]``).
        msg: Human-readable error message.
        type: Machine-readable error type (e.g. ``"value_error.missing"``).
    """

    loc: list[str | int]
    msg: str
    type: str
