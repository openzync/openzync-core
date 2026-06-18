"""Dialog classification model — intent, emotion, and sentiment classification
for individual episodes.

Classification results are produced by the enrichment pipeline and store
discrete labels (intent, emotion, valence, arousal) alongside confidence
scores and raw classifier output.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Float, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


class DialogClassification(TimestampMixin, Base):
    """Classification labels for a single episode.

    Attributes:
        id: UUID primary key.
        organization_id: Foreign key to the owning organization (tenant isolation).
        episode_id: Foreign key to the classified episode.
        intent: Predicted intent label (e.g., ``greeting``, ``question``,
            ``command``).
        emotion: Predicted emotion label (e.g., ``joy``, ``frustration``).
        valence: Sentiment valence (e.g., ``positive``, ``negative``,
            ``neutral``).
        arousal: Emotional arousal level (e.g., ``low``, ``medium``,
            ``high``).
        confidence: Classifier confidence score (0.0–1.0).
        raw: Raw classifier output (full JSON response from the model).
    """

    __tablename__ = "dialog_classifications"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Denormalized for efficient project-scoped queries without joining through episode.",
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    episode_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("episodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    intent: Mapped[str | None] = mapped_column(Text, nullable=True)
    emotion: Mapped[str | None] = mapped_column(Text, nullable=True)
    valence: Mapped[str | None] = mapped_column(Text, nullable=True)
    arousal: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.0,
        server_default="0.0",
    )
    raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<DialogClassification id={self.id} episode={self.episode_id} "
            f"intent={self.intent!r} emotion={self.emotion!r}>"
        )
