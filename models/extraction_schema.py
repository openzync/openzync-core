"""Extraction schema model — defines the JSON Schema contract for structured
extractions within an organization.

Each organization maintains its own catalog of extraction schemas. The
``json_schema`` field stores a JSON Schema document that ``StructuredExtraction``
payloads must conform to.
"""

import uuid

from sqlalchemy import Boolean, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


class ExtractionSchema(TimestampMixin, Base):
    """A named JSON Schema definition for structured extractions or classification schemas.

    Attributes:
        id: UUID primary key.
        organization_id: Foreign key to the owning organization.
        name: Human-readable schema name (unique within an organization).
        json_schema: The JSON Schema definition that extraction payloads
            must conform to. For ``type='classification'``, this stores the
            label definitions (intent, emotion, valence, arousal options).
        type: Schema type — ``'structured'`` (default) or ``'classification'``.
        prompt_template: Optional organization-specific prompt override
            for guiding the LLM extraction.
        is_active: Soft toggle — inactive schemas are not available for
            new extractions but existing references are preserved.
    """

    __tablename__ = "extraction_schemas"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="structured",
        server_default="structured",
    )
    json_schema: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
    )
    prompt_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "name",
            name="uq_extraction_schema_org_name",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<ExtractionSchema id={self.id} "
            f"org={self.organization_id} name={self.name!r}>"
        )
