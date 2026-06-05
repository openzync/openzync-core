"""Structured extraction model — schema-driven data extracted from sessions.

Each extraction captures structured data (as JSONB) conforming to an
``ExtractionSchema``. Extractions are linked to a session and optionally
to a specific schema definition.
"""

import uuid

from sqlalchemy import ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


class StructuredExtraction(TimestampMixin, Base):
    """A single structured extraction result from a session.

    Attributes:
        id: UUID primary key.
        session_id: Foreign key to the session this extraction belongs to.
        schema_id: Optional FK to the ``ExtractionSchema`` that defines
            the expected shape. Nullable to allow ad-hoc extractions.
        data: The extracted JSONB payload, conforming to the schema.
    """

    __tablename__ = "structured_extractions"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    schema_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("extraction_schemas.id", ondelete="SET NULL"),
        nullable=True,
    )
    data: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<StructuredExtraction id={self.id} "
            f"session={self.session_id} schema={self.schema_id}>"
        )
