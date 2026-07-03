"""Dialog classification worker — ARQ task that classifies episode content.

Runs after an episode is committed to PostgreSQL.  Uses an LLM to classify a
single conversation turn into intent, emotion, valence, and arousal labels
based on the organization's configured classification schemas.

Bitmask:
    Sets ``episodes.enrichment_status`` bit 4 (``ENRICHMENT_CLASSIFICATION``)
    on success or after a permanent failure.
"""

from __future__ import annotations

import orjson
import uuid
from typing import Any

import structlog
from sqlalchemy import text

from core.exceptions import EpisodeNotFoundError
from workers.tasks.base import ENRICHMENT_CLASSIFICATION, with_retry

from services.worker.prompt_renderer import build_enrichment_prompt, render_prompt

logger = structlog.get_logger()

ALLOWED_VALENCES = frozenset({"positive", "negative", "neutral"})
ALLOWED_AROUSALS = frozenset({"low", "medium", "high"})


# ── Public ARQ task (decorated with retry) ─────────────────────────────────────


@with_retry(max_retries=3, base_delay_s=2.0)
async def classify_dialog(
    ctx: object,
    episode_id: str,
    org_id: str,
    project_id: str,
    content: str,
    trace_id: str = "",
    session_id: str | None = None,
    user_id: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Classify a dialog turn and persist the result.

    This function is designed as an ARQ task — the ``ctx`` parameter provides
    a shared DB engine from the worker process (``ctx["db_engine"]``).
    When ``ctx`` is absent (direct invocation), a short-lived engine is
    created as a fallback.

    Pipeline:
        1. Create a temporary DB engine + session.
        2. Set ``app.org_id`` for RLS compliance.
        3. Check ``enrichment_status`` — skip if bit 4 is already set.
        4. Fetch organization's classification schemas (``type='classification'``).
        5. Extract label definitions from schemas (or use defaults).
        6. Resolve prompt template from DB (fall back to filesystem).
        7. Fetch custom instructions for the ``classification`` scope.
        8. Render the ``classify_dialog_v1.jinja2`` prompt (with DB
           template + custom instructions).
        9. Call the LLM backend (temperature 0.0, max_tokens 300).
        10. Parse and validate the JSON response.
        11. Insert a ``DialogClassification`` row.
        12. Update ``enrichment_status`` bit 4.

    Args:
        ctx: ARQ worker context (unused — required by ARQ contract).
        episode_id: UUID of the source episode (string, from ARQ).
        org_id: UUID of the owning organization.
        project_id: UUID of the project for project scoping.
        content: The message text to classify.
        trace_id: Request trace ID for end-to-end correlation across ARQ tasks.

    Raises:
        Exception: Re-raises the last LLM or DB error after retry exhaustion.
    """
    if trace_id:
        structlog.contextvars.bind_contextvars(trace_id=trace_id)

    # Lazy imports — ARQ workers run in a separate process.
    from core.db import get_async_session
    from core.llm import resolve_backend
    from core.org_config import get_org_config
    from repositories.episode_repository import EpisodeRepository
    from schemas.llm_outputs import ClassificationOutput

    logger.info(
        "classification.started",
        episode_id=episode_id,
        org_id=org_id,
        project_id=project_id,
        content_length=len(content),
        trace_id=trace_id,
    )

    # Use the shared engine from worker context.  ARQ workers running
    # under `services/worker/worker.py` receive this automatically.
    # The fallback path supports direct invocation (e.g. unit tests).
    engine = ctx.get("db_engine") if isinstance(ctx, dict) else None
    if engine is None:
        from core.config import settings as _settings
        from core.db import init_db_engine

        engine = init_db_engine(
            str(_settings.DATABASE_URL), pool_size=2, max_overflow=1
        )
        _own_engine = True
    else:
        _own_engine = False
    session_factory = ctx.get("db_session_factory") if isinstance(ctx, dict) else None
    if session_factory is None:
        session_factory = get_async_session(engine)

    try:
        async with session_factory() as db:
            # ── 2. Set RLS context ─────────────────────────────────────────
            await db.execute(
                text("SELECT set_config('app.org_id', :org_id, true)"),
                {"org_id": org_id},
            )

            # ── 3. Idempotency check — skip if already classified ──────────
            episode_repo = EpisodeRepository(db)
            episode = await episode_repo.get_by_id_for_update(uuid.UUID(episode_id))
            if episode is None:
                logger.warning(
                    "classification.episode_not_found",
                    episode_id=episode_id,
                )
                raise EpisodeNotFoundError(
                    message=f"Episode {episode_id} not found for classification.",
                    detail={"episode_id": episode_id},
                )
            if episode.enrichment_status & ENRICHMENT_CLASSIFICATION:
                logger.info(
                    "classification.skipped_already_done",
                    episode_id=episode_id,
                )
                return

            # ── 4. Render prompt (system instructions) with auto-injected context ──
            system_prompt, prompt_ctx = await render_prompt(
                "classification",
                org_id=org_id,
                episode_id=episode_id,
                user_id=user_id,
                session_id=session_id,
                db_session_factory=session_factory,
                return_context=True,
                metadata=metadata or {},
            )
            prompt = build_enrichment_prompt(system_prompt, prompt_ctx)

            # ── 7b. Fetch per-organization config ──────────────────────────
            org_cfg = await get_org_config(
                uuid.UUID(org_id), db, redis=None
            )
            llm_config_dict = org_cfg.to_llm_config_dict()

            # ── 8-9. Call LLM with structured-output validation ────────────
            try:
                llm = await resolve_backend(org_config=llm_config_dict)
                response = await llm.chat(
                    [
                        {
                            "role": "system",
                            "content": (
                                "You are a dialog classification system."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    response_model=ClassificationOutput,
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

            # Validate and normalise against org's label sets
            validation_sets = await _fetch_validation_sets(db, org_id)

            parsed = response.validated_data  # ClassificationOutput instance

            intent = _validate_label(
                parsed.intent, validation_sets["intent_set"]
            )
            emotion = _validate_label(
                parsed.emotion, validation_sets["emotion_set"]
            )
            valence = (
                parsed.valence if parsed.valence in ALLOWED_VALENCES else None
            )
            arousal = (
                parsed.arousal if parsed.arousal in ALLOWED_AROUSALS else None
            )
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

            # ── 11. Insert classification row ──────────────────────────────
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

            # ── 12. Set enrichment bit ─────────────────────────────────────
            await episode_repo.apply_enrichment_bits(
                uuid.UUID(episode_id), ENRICHMENT_CLASSIFICATION
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
        if _own_engine:
            await engine.dispose()


# ── Private helpers ────────────────────────────────────────────────────────────


async def _fetch_validation_sets(
    db: Any, org_id: str
) -> dict[str, set[str]]:
    """Fetch intent and emotion label sets from the org's schemas.

    Falls back to defaults when no schemas are configured.
    Returns ``{"intent_set": ..., "emotion_set": ...}``.
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
            "intent_set": {
                "greeting", "question", "command", "complaint",
                "chit-chat", "farewell", "request", "confirmation",
            },
            "emotion_set": {
                "joy", "frustration", "sadness", "anger",
                "neutral", "surprise", "fear", "disgust",
            },
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

    return {
        "intent_set": all_intents
        or {
            "greeting", "question", "command", "complaint",
            "chit-chat", "farewell", "request", "confirmation",
        },
        "emotion_set": all_emotions
        or {
            "joy", "frustration", "sadness", "anger",
            "neutral", "surprise", "fear", "disgust",
        },
    }


def _validate_label(label: Any, allowed_set: set[str]) -> str | None:
    """Validate that *label* is a non-empty string in *allowed_set*.

    Returns the label if valid, ``None`` otherwise.
    """
    if not isinstance(label, str) or not label.strip():
        return None
    return label if label in allowed_set else None



