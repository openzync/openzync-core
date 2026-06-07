"""Dialog classification worker — ARQ task that classifies episode content.

Runs after an episode is committed to PostgreSQL.  Uses an LLM to classify a
single conversation turn into intent, emotion, valence, and arousal labels
based on the organization's configured classification schemas.

Bitmask:
    Sets ``episodes.enrichment_status`` bit 4 (``ENRICHMENT_CLASSIFICATION``)
    on success or after a permanent failure.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import structlog
from sqlalchemy import text

from workers.tasks.base import ENRICHMENT_CLASSIFICATION, with_retry

from services.worker.prompt_renderer import render_prompt

logger = structlog.get_logger()

# ── Default label sets (used when no classification schemas are configured) ────

DEFAULT_INTENT_LABELS = "greeting, question, command, complaint, chit-chat, farewell, request, confirmation"
DEFAULT_EMOTION_LABELS = "joy, frustration, sadness, anger, neutral, surprise, fear, disgust"
DEFAULT_VALENCE_OPTIONS = "positive, negative, neutral"
DEFAULT_AROUSAL_OPTIONS = "low, medium, high"

ALLOWED_VALENCES = frozenset({"positive", "negative", "neutral"})
ALLOWED_AROUSALS = frozenset({"low", "medium", "high"})


# ── Public ARQ task (decorated with retry) ─────────────────────────────────────


@with_retry(max_retries=3, base_delay_s=2.0)
async def classify_dialog(
    ctx: object,
    episode_id: str,
    org_id: str,
    content: str,
) -> None:
    """Classify a dialog turn and persist the result.

    This function is designed as an ARQ task — the ``ctx`` parameter is
    required by the ARQ contract but is not used directly here (we create
    a short-lived DB engine per invocation).

    Pipeline:
        1. Create a temporary DB engine + session.
        2. Set ``app.org_id`` for RLS compliance.
        3. Check ``enrichment_status`` — skip if bit 4 is already set.
        4. Fetch organization's classification schemas (``type='classification'``).
        5. Extract label definitions from schemas (or use defaults).
        6. Render the ``classify_dialog_v1.jinja2`` prompt.
        7. Call the LLM backend (temperature 0.0, max_tokens 300).
        8. Parse and validate the JSON response.
        9. Insert a ``DialogClassification`` row.
        10. Update ``enrichment_status`` bit 4.

    Args:
        ctx: ARQ worker context (unused — required by ARQ contract).
        episode_id: UUID of the source episode (string, from ARQ).
        org_id: UUID of the owning organization.
        content: The message text to classify.

    Raises:
        Exception: Re-raises the last LLM or DB error after retry exhaustion.
    """
    # Lazy imports — ARQ workers run in a separate process.
    from core.config import settings
    from core.db import get_async_session, init_db_engine
    from core.llm import resolve_backend

    logger.info(
        "classification.started",
        episode_id=episode_id,
        org_id=org_id,
        content_length=len(content),
    )

    engine = None
    try:
        # ── 1. Create temporary DB engine ──────────────────────────────────
        engine = init_db_engine(
            str(settings.DATABASE_URL), pool_size=2, max_overflow=1
        )
        session_factory = get_async_session(engine)

        async with session_factory() as db:
            # ── 2. Set RLS context ─────────────────────────────────────────
            await db.execute(
                text("SELECT set_config('app.org_id', :org_id, true)"),
                {"org_id": org_id},
            )

            # ── 3. Idempotency check — skip if already classified ──────────
            result = await db.execute(
                text(
                    "SELECT enrichment_status FROM episodes "
                    "WHERE id = :episode_id FOR UPDATE"
                ),
                {"episode_id": uuid.UUID(episode_id)},
            )
            row = result.one_or_none()
            if row is None:
                logger.warning(
                    "classification.episode_not_found",
                    episode_id=episode_id,
                )
                return
            current_status: int = row[0]
            if current_status & ENRICHMENT_CLASSIFICATION:
                logger.info(
                    "classification.skipped_already_done",
                    episode_id=episode_id,
                )
                return

            # ── 4. Fetch org classification schemas ────────────────────────
            labels = await _fetch_classification_labels(db, org_id)

            # ── 5. Render prompt ───────────────────────────────────────────
            prompt = render_prompt(
                "classify_dialog_v1",
                conversation=content,
                intent_labels=labels["intent_labels"],
                emotion_labels=labels["emotion_labels"],
                valence_options=labels["valence_options"],
                arousal_options=labels["arousal_options"],
            )

            # ── 6. Call LLM ────────────────────────────────────────────────
            try:
                llm = await resolve_backend()
                response = await llm.chat(
                    [
                        {
                            "role": "system",
                            "content": (
                                "You are a dialog classification system. "
                                "Output ONLY valid JSON."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.0,
                    max_tokens=300,
                )
            except Exception as exc:
                logger.error(
                    "classification.llm_failed",
                    episode_id=episode_id,
                    error=str(exc),
                )
                raise  # Let @with_retry handle transient failures

            # ── 7. Parse JSON response ─────────────────────────────────────
            parsed = _parse_classification_response(response.content)

            # Recovery attempt if first parse failed
            if parsed is None:
                logger.warning(
                    "classification.parse_recovery",
                    episode_id=episode_id,
                )
                try:
                    response2 = await llm.chat(
                        [
                            {
                                "role": "system",
                                "content": (
                                    "CRITICAL: You MUST output valid JSON only. "
                                    "No other text, no markdown fences, "
                                    "no explanation."
                                ),
                            },
                            {"role": "user", "content": prompt},
                        ],
                        temperature=0.0,
                        max_tokens=300,
                    )
                    parsed = _parse_classification_response(response2.content)
                except Exception as exc:
                    logger.error(
                        "classification.recovery_failed",
                        episode_id=episode_id,
                        error=str(exc),
                    )

            # ── 8. Validate labels against allowed sets ─────────────────────
            intent = None
            emotion = None
            valence = None
            arousal = None
            confidence = 0.0
            raw = None

            if parsed is not None:
                intent = _validate_label(
                    parsed.get("intent"), labels["intent_set"]
                )
                emotion = _validate_label(
                    parsed.get("emotion"), labels["emotion_set"]
                )
                valence_raw = parsed.get("valence")
                valence = (
                    valence_raw if valence_raw in ALLOWED_VALENCES else None
                )
                arousal_raw = parsed.get("arousal")
                arousal = (
                    arousal_raw if arousal_raw in ALLOWED_AROUSALS else None
                )
                confidence = min(max(float(parsed.get("confidence", 0.0)), 0.0), 1.0)
                raw = parsed

                if intent is None and parsed.get("intent") is not None:
                    logger.warning(
                        "classification.invalid_intent",
                        episode_id=episode_id,
                        received=parsed.get("intent"),
                        allowed=list(labels["intent_set"]),
                    )
                if emotion is None and parsed.get("emotion") is not None:
                    logger.warning(
                        "classification.invalid_emotion",
                        episode_id=episode_id,
                        received=parsed.get("emotion"),
                        allowed=list(labels["emotion_set"]),
                    )

            # ── 9. Insert classification row ───────────────────────────────
            await db.execute(
                text("""
                    INSERT INTO dialog_classifications
                        (organization_id, episode_id, intent, emotion,
                         valence, arousal, confidence, raw,
                         created_at, updated_at)
                    VALUES
                        (:org_id, :episode_id, :intent, :emotion,
                         :valence, :arousal, :confidence, :raw::jsonb,
                         now(), now())
                """),
                {
                    "org_id": uuid.UUID(org_id),
                    "episode_id": uuid.UUID(episode_id),
                    "intent": intent,
                    "emotion": emotion,
                    "valence": valence,
                    "arousal": arousal,
                    "confidence": confidence,
                    "raw": json.dumps(raw) if raw else None,
                },
            )

            # ── 10. Set enrichment bit ─────────────────────────────────────
            await db.execute(
                text("""
                    UPDATE episodes
                    SET enrichment_status = enrichment_status | :bit
                    WHERE id = :episode_id
                """),
                {
                    "bit": ENRICHMENT_CLASSIFICATION,
                    "episode_id": uuid.UUID(episode_id),
                },
            )

            await db.commit()

            logger.info(
                "classification.completed",
                episode_id=episode_id,
                intent=intent,
                emotion=emotion,
                valence=valence,
                arousal=arousal,
                confidence=confidence,
            )

    except Exception:
        logger.error(
            "classification.failed",
            episode_id=episode_id,
            org_id=org_id,
        )
        raise
    finally:
        if engine is not None:
            await engine.dispose()


# ── Private helpers ────────────────────────────────────────────────────────────


async def _fetch_classification_labels(
    db: Any, org_id: str
) -> dict[str, Any]:
    """Fetch classification label definitions from the org's schemas.

    Queries ``extraction_schemas`` where ``type='classification'`` and
    ``is_active=true``.  Merges label sets if multiple schemas exist.

    Returns a dict with:
        ``intent_labels``: comma-separated string for prompt injection.
        ``emotion_labels``: comma-separated string for prompt injection.
        ``valence_options``: comma-separated string for prompt injection.
        ``arousal_options``: comma-separated string for prompt injection.
        ``intent_set``: set of allowed intent values (for validation).
        ``emotion_set``: set of allowed emotion values (for validation).
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
        return {
            "intent_labels": DEFAULT_INTENT_LABELS,
            "emotion_labels": DEFAULT_EMOTION_LABELS,
            "valence_options": DEFAULT_VALENCE_OPTIONS,
            "arousal_options": DEFAULT_AROUSAL_OPTIONS,
            "intent_set": _parse_label_set(DEFAULT_INTENT_LABELS),
            "emotion_set": _parse_label_set(DEFAULT_EMOTION_LABELS),
        }

    # Merge labels from all active classification schemas
    all_intents: set[str] = set()
    all_emotions: set[str] = set()
    valences: set[str] = set()
    arousals: set[str] = set()

    for row in schemas:
        schema: dict = row[0]
        if isinstance(schema, dict):
            if "intent" in schema and isinstance(schema["intent"], list):
                all_intents.update(schema["intent"])
            if "emotion" in schema and isinstance(schema["emotion"], list):
                all_emotions.update(schema["emotion"])
            if "valence" in schema and isinstance(schema["valence"], list):
                valences.update(schema["valence"])
            if "arousal" in schema and isinstance(schema["arousal"], list):
                arousals.update(schema["arousal"])

    return {
        "intent_labels": ", ".join(sorted(all_intents)) if all_intents else DEFAULT_INTENT_LABELS,
        "emotion_labels": ", ".join(sorted(all_emotions)) if all_emotions else DEFAULT_EMOTION_LABELS,
        "valence_options": ", ".join(sorted(valences)) if valences else DEFAULT_VALENCE_OPTIONS,
        "arousal_options": ", ".join(sorted(arousals)) if arousals else DEFAULT_AROUSAL_OPTIONS,
        "intent_set": all_intents or _parse_label_set(DEFAULT_INTENT_LABELS),
        "emotion_set": all_emotions or _parse_label_set(DEFAULT_EMOTION_LABELS),
    }


def _parse_classification_response(content: str) -> dict | None:
    """Parse LLM JSON response for classification.

    Handles markdown code fences, trailing commas, and extra text before/after.

    Args:
        content: Raw response text from the LLM.

    Returns:
        A dict with classification fields, or ``None`` if parsing failed.
    """
    # Strip markdown code fences
    if "```json" in content:
        content = content.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in content:
        content = content.split("```", 1)[1].split("```", 1)[0].strip()

    content = content.strip()

    # Find the first JSON object
    json_start = content.find("{")
    if json_start < 0:
        return None
    content = content[json_start:]

    # Find matching closing brace
    depth = 0
    for i, ch in enumerate(content):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                content = content[: i + 1]
                break

    if not content:
        return None

    try:
        data: dict = json.loads(content)
    except json.JSONDecodeError:
        logger.warning(
            "classification.parse_failed",
            content_preview=content[:300],
        )
        return None

    if not isinstance(data, dict):
        return None

    return data


def _validate_label(label: Any, allowed_set: set[str]) -> str | None:
    """Validate that *label* is a non-empty string in *allowed_set*.

    Returns the label if valid, ``None`` otherwise.
    """
    if not isinstance(label, str) or not label.strip():
        return None
    return label if label in allowed_set else None


def _parse_label_set(labels_csv: str) -> set[str]:
    """Convert a comma-separated label string into a set of stripped labels."""
    return {label.strip() for label in labels_csv.split(",") if label.strip()}
