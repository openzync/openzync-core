"""GraphEntity model — minimal definition to satisfy FK resolution.

The ``graph_entities`` table is created via raw SQL in migration 0006
and managed primarily through the Graphiti backend client. This model
exists only so that SQLAlchemy can resolve the
``ForeignKey("graph_entities.id")`` references in ``Fact`` and other
models. No business logic, no repository.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Text, TIMESTAMP, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class GraphEntity(Base):
    """Maps to the ``graph_entities`` table created in migration 0006.

    .. caution::
       This is a **read-only stub model**.  All writes to ``graph_entities``
       go through the Graphiti backend client or raw SQL in worker tasks.
       No ORM-based repository or service layer should mutate this table.
    """

    __tablename__ = "graph_entities"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="custom",
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    attributes: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default="{}",
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<GraphEntity id={self.id} name={self.name!r} "
            f"type={self.entity_type!r}>"
        )
