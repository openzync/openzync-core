"""Dialog classification helpers for the combined enrichment worker.

Exports ``process_classification_output`` (plus label-validation helpers),
which the combined ``enrich_episode`` worker calls to persist LLM
classification output.  The standalone ``classify_dialog`` ARQ task was
retired in favour of ``enrich_episode``.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import orjson
import structlog
from sqlalchemy import text

from workers.tasks.base import ENRICHMENT_CLASSIFICATION

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from repositories.episode_repository import EpisodeRepository
    from schemas.llm_outputs import ClassificationOutput

logger = structlog.get_logger()

ALLOWED_VALENCES = frozenset({"positive", "negative", "neutral"})
ALLOWED_AROUSALS = frozenset({"low", "medium", "high"})


# ── Private helpers ────────────────────────────────────────────────────────────


async def _fetch_validation_sets(
    db: Any, org_id: str
) -> dict[str, set[str]]:
    """Fetch intent and emotion label sets from the org's schemas.

    Returns ``{"intent_set": ..., "emotion_set": ...}`` with possibly
    empty sets.  The caller is responsible for handling empty sets
    (e.g. by raising or falling back at a higher level).
    """
    result = await db.execute(
        text("""
            SELECT json_schema FROM extraction_schemas
            WHERE organization_id = :org_id
              AND type = 'classification'
              AND is_active = true
        """),
        {"org_id": uuid.UUID(org_id)},
    )
    schemas = result.all()

    if not schemas:
        logger.warning(
            "classify_dialog.no_classification_schemas",
            org_id=org_id,
        )
        return {
            "intent_set": set(),
            "emotion_set": set(),
        }

    all_intents: set[str] = set()
    all_emotions: set[str] = set()
    for row in schemas:
        schema: dict = row[0]
        if isinstance(schema, dict):
            if "intent" in schema and isinstance(schema["intent"], list):
                all_intents.update(schema["intent"])
            if "emotion" in schema and isinstance(schema["emotion"], list):
                all_emotions.update(schema["emotion"])

    if not all_intents:
        logger.warning(
            "classify_dialog.no_intents_found",
            org_id=org_id,
            schema_count=len(schemas),
        )
    if not all_emotions:
        logger.warning(
            "classify_dialog.no_emotions_found",
            org_id=org_id,
            schema_count=len(schemas),
        )

    return {
        "intent_set": all_intents,
        "emotion_set": all_emotions,
    }


def _validate_label(label: Any, allowed_set: set[str]) -> str | None:
    """Validate that *label* is a non-empty string in *allowed_set*.

    Returns the label if valid, ``None`` otherwise.
    """
    if not isinstance(label, str) or not label.strip():
        return None
    return label if label in allowed_set else None


# ── Exportable post-processing helper (used by combined worker) ─────────────


async def process_classification_output(
    db: AsyncSession,
    org_id: str,
    episode_id: str,
    project_id: str,
    parsed: ClassificationOutput,
    validation_sets: dict[str, set[str]] | None = None,
    episode_repo: EpisodeRepository | None = None,
) -> None:
    """Validate and persist classification output from LLM.

    This function is called by both the standalone ``classify_dialog`` task
    and the combined ``enrich_episode`` worker.  It does **not** manage
    transactions — the caller is responsible for commit/rollback.

    Args:
        db: Database session (caller manages transaction).
        org_id: Organization UUID string.
        episode_id: Episode UUID string.
        project_id: Project UUID string.
        parsed: The validated ``ClassificationOutput`` from the LLM.
        validation_sets: Optional pre-fetched label validation sets.
            If ``None``, fetches from DB.  Cache-friendly for callers
            that already have this data.
        episode_repo: Optional repository for setting enrichment bits.
            If ``None``, enrichment bits are not set (caller manages this).

    Raises:
        Various DB errors on insert failure.
    """
    if validation_sets is None:
        validation_sets = await _fetch_validation_sets(db, org_id)

    intent = _validate_label(parsed.intent, validation_sets["intent_set"])
    emotion = _validate_label(parsed.emotion, validation_sets["emotion_set"])
    valence = parsed.valence if parsed.valence in ALLOWED_VALENCES else None
    arousal = parsed.arousal if parsed.arousal in ALLOWED_AROUSALS else None
    confidence = min(max(parsed.confidence, 0.0), 1.0)
    raw = parsed.model_dump()

    if intent is None and parsed.intent is not None:
        logger.warning(
            "classification.invalid_intent",
            episode_id=episode_id,
            received=parsed.intent,
            allowed=list(validation_sets["intent_set"]),
        )
    if emotion is None and parsed.emotion is not None:
        logger.warning(
            "classification.invalid_emotion",
            episode_id=episode_id,
            received=parsed.emotion,
            allowed=list(validation_sets["emotion_set"]),
        )

    # ── Insert classification row ──────────────────────────────────────────
    await db.execute(
        text("""
            INSERT INTO dialog_classifications
                (organization_id, episode_id, project_id, intent,
                 emotion, valence, arousal, confidence, raw,
                 created_at, updated_at)
            VALUES
                (:org_id, :episode_id, :project_id, :intent,
                 :emotion, :valence, :arousal, :confidence,
                 CAST(:raw AS jsonb), now(), now())
            ON CONFLICT (organization_id, episode_id) DO NOTHING
        """),
        {
            "org_id": uuid.UUID(org_id),
            "episode_id": uuid.UUID(episode_id),
            "project_id": uuid.UUID(project_id),
            "intent": intent,
            "emotion": emotion,
            "valence": valence,
            "arousal": arousal,
            "confidence": confidence,
            "raw": orjson.dumps(raw).decode("utf-8") if raw else None,
        },
    )

    # ── Set enrichment bit (caller may handle this) ────────────────────────
    if episode_repo is not None:
        await episode_repo.apply_enrichment_bits(
            uuid.UUID(episode_id), ENRICHMENT_CLASSIFICATION
        )

    await db.flush()


