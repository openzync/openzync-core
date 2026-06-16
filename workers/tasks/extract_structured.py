"""Structured extraction worker — ARQ task that extracts structured data from episodes.

Runs after an episode is committed to PostgreSQL.  Uses an LLM to extract
structured data conforming to the organization's configured JSON Schemas
(``extraction_schemas`` where ``type='structured'``).

Bitmask:
    Sets ``episodes.enrichment_status`` bit 5 (``ENRICHMENT_STRUCTURED_EXTRACTION``)
    on success or after a permanent failure.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import structlog
from sqlalchemy import text

from workers.tasks.base import ENRICHMENT_STRUCTURED_EXTRACTION, with_retry

from services.worker.prompt_renderer import build_enrichment_prompt, render_prompt

logger = structlog.get_logger()


@with_retry(max_retries=3, base_delay_s=2.0)
async def extract_structured(
    ctx: object,
    episode_id: str,
    org_id: str,
    user_id: str,
    session_id: str,
    content: str,
    trace_id: str = "",
    metadata: dict | None = None,
) -> None:
    """Extract structured data from a dialog turn and persist the result.

    Pipeline:
        1. Create a temporary DB engine + session.
        2. Set ``app.org_id`` for RLS compliance.
        3. Check ``enrichment_status`` — skip if bit 5 is already set.
        4. Fetch organization's structured schemas (``type='structured'``).
        5. If no schemas configured, set the bit and return (nothing to extract).
        6. Resolve prompt template from DB (fall back to filesystem).
        7. Fetch custom instructions for the ``extraction`` scope.
        8. Render the ``extract_structured_v1.jinja2`` prompt (with DB
           template + custom instructions).
        9. Call the LLM backend (temperature 0.0, max_tokens configurable).
        10. Parse the keyed JSON response — each key is a schema name.
        11. For each matched schema, validate output against the JSON Schema.
        12. Insert one ``StructuredExtraction`` row per valid schema.
        13. Update ``enrichment_status`` bit 5.

    Args:
        ctx: ARQ worker context (unused — required by ARQ contract).
        episode_id: UUID of the source episode (string, from ARQ).
        org_id: UUID of the owning organization.
        user_id: UUID of the user (for episode FK context).
        session_id: UUID of the session (for FK to structured_extractions).
        content: The message text to extract data from.
        trace_id: Request trace ID for end-to-end correlation across ARQ tasks.

    Raises:
        Exception: Re-raises the last LLM or DB error after retry exhaustion.
    """
    if trace_id:
        structlog.contextvars.bind_contextvars(trace_id=trace_id)

    # Lazy imports — ARQ workers run in a separate process.
    from core.config import settings
    from core.db import get_async_session
    from core.llm import resolve_backend
    from core.org_config import get_org_config
    from services.worker.worker_settings import settings as worker_settings

    logger.info(
        "structured_extraction.started",
        episode_id=episode_id,
        org_id=org_id,
        session_id=session_id,
        content_length=len(content),
        trace_id=trace_id,
    )

    # Use the shared engine from worker context.
    engine = ctx.get("db_engine") if isinstance(ctx, dict) else None
    if engine is None:
        from core.db import init_db_engine

        engine = init_db_engine(
            str(settings.DATABASE_URL), pool_size=2, max_overflow=1
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

            # ── 3. Idempotency check — skip if already extracted ──────────
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
                    "structured_extraction.episode_not_found",
                    episode_id=episode_id,
                )
                return
            current_status: int = row[0]
            if current_status & ENRICHMENT_STRUCTURED_EXTRACTION:
                logger.info(
                    "structured_extraction.skipped_already_done",
                    episode_id=episode_id,
                )
                return

            # ── 4. Render prompt (system instructions) with auto-injected context ──
            system_prompt, prompt_ctx = await render_prompt(
                "structured_extraction",
                org_id=org_id,
                episode_id=episode_id,
                session_id=session_id,
                user_id=user_id,
                db_session_factory=session_factory,
                return_context=True,
                metadata=metadata or {},
            )
            prompt = build_enrichment_prompt(system_prompt, prompt_ctx)

            schemas: list[dict] = prompt_ctx.get("schemas", [])
            if not schemas:
                logger.info(
                    "structured_extraction.no_schemas",
                    episode_id=episode_id,
                )
                # No schemas configured — set the enrichment bit and return.
                await db.execute(
                    text("""
                        UPDATE episodes
                        SET enrichment_status = enrichment_status | :bit
                        WHERE id = :episode_id
                    """),
                    {
                        "bit": ENRICHMENT_STRUCTURED_EXTRACTION,
                        "episode_id": uuid.UUID(episode_id),
                    },
                )
                await db.commit()
                return

            max_tokens = worker_settings.STRUCTURED_EXTRACTION_MAX_TOKENS

            # ── 7b. Fetch per-organization config ──────────────────────────
            org_cfg = await get_org_config(
                uuid.UUID(org_id), db, redis=None
            )
            llm_config_dict = org_cfg.to_llm_config_dict()

            # ── 8. Call LLM ────────────────────────────────────────────────
            try:
                llm = await resolve_backend(org_config=llm_config_dict)
                response = await llm.chat(
                    [
                        {
                            "role": "system",
                            "content": (
                                "You are a structured data extraction system. "
                                "Output ONLY valid JSON."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.0,
                    max_tokens=max_tokens,
                )
            except Exception as exc:
                logger.error(
                    "structured_extraction.llm_failed",
                    episode_id=episode_id,
                    error=str(exc),
                )
                raise  # Let @with_retry handle transient failures

            # ── 9. Parse JSON response ─────────────────────────────────────
            parsed = _parse_structured_response(response.content)

            # Recovery attempt if first parse failed
            if parsed is None:
                logger.warning(
                    "structured_extraction.parse_recovery",
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
                        max_tokens=max_tokens,
                    )
                    parsed = _parse_structured_response(response2.content)
                except Exception as exc:
                    logger.error(
                        "structured_extraction.recovery_failed",
                        episode_id=episode_id,
                        error=str(exc),
                    )

            # ── 10. Validate & insert per schema ───────────────────────────
            schema_map: dict[str, dict[str, Any]] = {
                s["name"]: s for s in schemas
            }

            if parsed and isinstance(parsed, dict):
                inserted_count = 0
                for schema_name, data in parsed.items():
                    schema_info = schema_map.get(schema_name)
                    if schema_info is None:
                        logger.warning(
                            "structured_extraction.unknown_schema",
                            episode_id=episode_id,
                            schema_name=schema_name,
                        )
                        continue

                    if data is None:
                        continue

                    if not isinstance(data, dict):
                        logger.warning(
                            "structured_extraction.non_dict_data",
                            episode_id=episode_id,
                            schema_name=schema_name,
                        )
                        continue

                    cleaned: dict[str, object] = {
                        k: v for k, v in data.items() if v is not None
                    }

                    TYPE_DEFAULTS: dict[str, object] = {
                        "string": "unknown",
                        "number": 0,
                        "integer": 0,
                        "boolean": False,
                    }
                    schema_obj: dict[str, object] = schema_info["json_schema"]
                    for field in schema_obj.get("required", []):
                        if field not in cleaned:
                            ftype: str = (
                                schema_obj.get("properties", {})
                                .get(field, {})
                                .get("type", "string")
                            )
                            cleaned[field] = TYPE_DEFAULTS.get(ftype, "unknown")

                    try:
                        _validate_against_schema(cleaned, schema_info["json_schema"])
                    except Exception as exc:
                        logger.warning(
                            "structured_extraction.validation_failed",
                            episode_id=episode_id,
                            schema_name=schema_name,
                            error=str(exc),
                        )
                        continue

                    await db.execute(
                        text("""
                            INSERT INTO structured_extractions
                                (organization_id, session_id, episode_id, schema_id, data,
                                 created_at, updated_at)
                            VALUES
                                (:org_id, :session_id, :episode_id, :schema_id, CAST(:data AS jsonb),
                                 now(), now())
                            ON CONFLICT (episode_id, schema_id)
                            DO UPDATE SET data = CAST(:data AS jsonb),
                                          updated_at = now()
                        """),
                        {
                            "org_id": uuid.UUID(org_id),
                            "session_id": uuid.UUID(session_id),
                            "episode_id": uuid.UUID(episode_id),
                            "schema_id": uuid.UUID(schema_info["id"]),
                            "data": json.dumps(cleaned),
                        },
                    )
                    inserted_count += 1

                logger.info(
                    "structured_extraction.inserted",
                    episode_id=episode_id,
                    count=inserted_count,
                )
            else:
                logger.info(
                    "structured_extraction.no_valid_output",
                    episode_id=episode_id,
                )

            # ── 11. Set enrichment bit ─────────────────────────────────────
            await db.execute(
                text("""
                    UPDATE episodes
                    SET enrichment_status = enrichment_status | :bit
                    WHERE id = :episode_id
                """),
                {
                    "bit": ENRICHMENT_STRUCTURED_EXTRACTION,
                    "episode_id": uuid.UUID(episode_id),
                },
            )

            await db.commit()

            logger.info(
                "structured_extraction.completed",
                episode_id=episode_id,
            )

    except Exception:
        logger.error(
            "structured_extraction.failed",
            episode_id=episode_id,
            org_id=org_id,
        )
        raise
    finally:
        if _own_engine:
            await engine.dispose()


# ── Private helpers ────────────────────────────────────────────────────────────


def _parse_structured_response(content: str) -> dict[str, Any] | None:
    """Parse LLM JSON response for structured extraction.

    Handles markdown code fences, trailing commas, and extra text before/after.

    Returns:
        A dict keyed by schema name, or ``None`` if parsing failed.
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
        data: dict[str, Any] = json.loads(content)
    except json.JSONDecodeError:
        logger.warning(
            "structured_extraction.parse_failed",
            content_preview=content[:300],
        )
        return None

    if not isinstance(data, dict):
        return None

    return data


def _validate_against_schema(data: dict, schema: dict) -> None:
    """Validate extracted data against a JSON Schema.

    Uses ``jsonschema.validate()``.  Raises on validation failure.

    Args:
        data: The extracted data to validate.
        schema: The JSON Schema definition to validate against.

    Raises:
        jsonschema.ValidationError: If the data does not conform to the schema.
    """
    # Lazy import since jsonschema may not always be needed
    import jsonschema  # noqa: PLC0415 — optional dependency

    jsonschema.validate(data, schema)
