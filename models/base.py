"""Declarative base and timestamp mixins for all OpenZync ORM models.

All domain models inherit from ``Base``. Mutable entities use ``TimestampMixin``
(created_at + updated_at). Append-only entities (audit_log, llm_usage) use
``CreatedAtMixin`` (created_at only).
"""

from datetime import datetime

from sqlalchemy import TIMESTAMP, func
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column


class Base(DeclarativeBase):
    """Shared declarative base for all OpenZync ORM models."""

    pass


class TimestampMixin:
    """Adds ``created_at`` and ``updated_at`` timestamp columns.

    ``updated_at`` is automatically updated on row modification via
    ``onupdate=func.now()`` at the SQLAlchemy level.
    """

    @declared_attr
    def created_at(cls) -> Mapped[datetime]:
        return mapped_column(
            TIMESTAMP(timezone=True),
            server_default=func.now(),
            nullable=False,
        )

    @declared_attr
    def updated_at(cls) -> Mapped[datetime]:
        return mapped_column(
            TIMESTAMP(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        )


class CreatedAtMixin:
    """Adds only ``created_at`` — for immutable / append-only tables.

    These tables must never be updated; ``updated_at`` is intentionally absent
    to enforce immutability at the schema level.
    """

    @declared_attr
    def created_at(cls) -> Mapped[datetime]:
        return mapped_column(
            TIMESTAMP(timezone=True),
            server_default=func.now(),
            nullable=False,
        )
