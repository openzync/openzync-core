"""Structured-extraction helpers for the combined enrichment worker.

Exports ``process_structured_output``, which the combined ``enrich_episode``
worker calls to persist LLM-extracted structured data against the
organization's configured JSON Schemas.  The standalone ``extract_structured``
ARQ task was retired in favour of ``enrich_episode``.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import orjson
import structlog
from sqlalchemy import text

from workers.tasks.base import ENRICHMENT_STRUCTURED_EXTRACTION

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from repositories.episode_repository import EpisodeRepository

logger = structlog.get_logger()

PREVIEW_SCHEMA_NAME = "preview"
"""Schema entry name used when rendering a single-schema extraction prompt."""

_PROMPTS_DIR = (
    Path(__file__).resolve().parent.parent.parent / "services" / "worker" / "prompts"
)
"""On-disk prompt catalogue (same files seeded into the DB at signup)."""


def _load_extraction_template(template_text: str | None) -> str:
    """Return the extraction template text, defaulting to the on-disk file.

    Args:
        template_text: Optional raw Jinja2 override (e.g. a per-schema
            ``prompt_template``).  When ``None`` the canonical
            ``extract_structured_v1.jinja2`` file is read from disk.

    Returns:
        The raw Jinja2 template text.
    """
    if template_text:
        return template_text
    return (_PROMPTS_DIR / "extract_structured_v1.jinja2").read_text()


def build_extraction_prompt(
    schema: dict, text: str, *, template_text: str | None = None
) -> str:
    """Render the structured-extraction prompt for one schema and one text.

    Renders ``extract_structured_v1.jinja2`` with the same variables the
    combined ``enrich_episode`` pass uses: ``schemas`` (a one-element list
    of ``{name, json_schema}`` dicts) and ``conversation`` (the raw text).

    Args:
        schema: JSON Schema dict describing the fields to extract.
        text: Raw conversation or sample text to extract from.
        template_text: Optional raw Jinja2 override.  Defaults to the
            on-disk ``extract_structured_v1.jinja2``.

    Returns:
        The rendered prompt string, ready for the LLM.
    """
    from jinja2 import Environment  # noqa: PLC0415 — render-only dependency

    # S701: plain-text LLM prompt, not HTML — escaping would corrupt schema JSON.
    template = Environment(autoescape=False).from_string(  # noqa: S701
        _load_extraction_template(template_text)
    )
    return template.render(
        schemas=[{"name": PREVIEW_SCHEMA_NAME, "json_schema": schema}],
        conversation=text,
    )


def collect_schema_validation_errors(data: dict, schema: dict) -> list[str]:
    """Validate *data* against *schema*, returning field-level error strings.

    Non-raising counterpart to :func:`_validate_against_schema` for paths
    (like schema preview) that report mismatches instead of failing.

    Args:
        data: The extracted data to validate.
        schema: The JSON Schema definition to validate against.

    Returns:
        Human-readable ``"<dotted.path>: <message>"`` strings sorted by
        path (bare message for root-level errors); empty when *data*
        conforms to *schema*.
    """
    import jsonschema  # noqa: PLC0415 — optional dependency

    validator = jsonschema.Draft7Validator(schema)
    errors = sorted(
        validator.iter_errors(data),
        key=lambda e: [str(p) for p in e.path],
    )
    formatted: list[str] = []
    for error in errors:
        path = ".".join(str(p) for p in error.absolute_path)
        formatted.append(f"{path}: {error.message}" if path else error.message)
    return formatted


def parse_extraction_json(raw: str) -> Any:
    """Parse LLM extraction output into a JSON value, tolerating fences.

    Strips markdown code fences, skips leading prose up to the first JSON
    object or array, and parses it.  Returns ``None`` when no parseable
    value is found — including when trailing garbage follows the value.
    A truncated or garbage-suffixed payload is never half-parsed: a
    partial object would write wrong fields downstream, so the whole
    extraction is rejected instead.

    Args:
        raw: Raw LLM response text.

    Returns:
        The parsed JSON value (usually a ``dict``), or ``None``.
        ``Any`` because the LLM may emit any JSON shape.
    """
    import orjson  # noqa: PLC0415 — parse-only dependency

    text = raw.strip()
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0].strip()

    brace = text.find("{")
    bracket = text.find("[")
    if bracket >= 0 and (brace < 0 or bracket < brace):
        brace = bracket
    if brace < 0:
        return None
    text = text[brace:]

    try:
        return orjson.loads(text.encode())
    except orjson.JSONDecodeError:
        return None


# ── Private helpers ────────────────────────────────────────────────────────────


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


async def process_structured_output(
    db: AsyncSession,
    org_id: str,
    episode_id: str,
    project_id: str,
    session_id: str,
    parsed: dict[str, Any],
    schemas: list[dict],
    episode_repo: EpisodeRepository | None = None,
) -> None:
    """Validate and persist structured extraction output from LLM.

    Validates extracted data against org-defined JSON schemas and
    upserts structured_extractions rows. Does NOT manage transactions
    — caller is responsible for commit/rollback.

    Args:
        db: Active database session.
        org_id: Organization UUID string.
        episode_id: Episode UUID string.
        project_id: Project UUID string.
        session_id: Session UUID string.
        parsed: The raw dict from the LLM (StructuredExtractionOutput
            accepts extra keys so its model_dump() is a dict).
        schemas: List of active extraction schema dicts from org config.
            Entries with no matching schema are skipped; an empty
            ``parsed`` writes no rows but still stamps the bit
            (assessed, nothing matched).
        episode_repo: Optional episode repository for setting enrichment bits.
            If None, bits are not set (caller manages this).

    Raises:
        Various DB errors on persistence failure.
    """
    if parsed:
        schema_map: dict[str, dict[str, Any]] = {s["name"]: s for s in schemas}

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

            # Missing required fields write zero rows: filling "unknown" /
            # 0 / False fabricates data the LLM never extracted.  The
            # schema is skipped loudly so the gap stays visible.
            schema_obj: dict[str, object] = schema_info["json_schema"]
            missing_fields = [
                field
                for field in schema_obj.get("required", [])
                if field not in cleaned
            ]
            if missing_fields:
                logger.warning(
                    "structured_extraction.missing_required_fields",
                    episode_id=episode_id,
                    schema_name=schema_name,
                    missing_fields=missing_fields,
                )
                continue

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
                        (organization_id, project_id, session_id, episode_id,
                         schema_id, data, created_at, updated_at)
                    VALUES
                        (:org_id, :project_id, :session_id, :episode_id,
                         :schema_id, CAST(:data AS jsonb),
                         now(), now())
                    ON CONFLICT (episode_id, schema_id)
                    DO UPDATE SET data = CAST(:data AS jsonb),
                                  updated_at = now()
                """),
                {
                    "org_id": uuid.UUID(org_id),
                    "project_id": uuid.UUID(project_id),
                    "session_id": uuid.UUID(session_id),
                    "episode_id": uuid.UUID(episode_id),
                    "schema_id": uuid.UUID(schema_info["id"]),
                    "data": orjson.dumps(cleaned).decode("utf-8"),
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

    # Structured bit — stamped unconditionally: an episode whose
    # structured output was extracted and assessed (or skipped because
    # empty) must not re-enrich.
    if episode_repo is not None:
        await episode_repo.apply_enrichment_bits(
            uuid.UUID(episode_id), ENRICHMENT_STRUCTURED_EXTRACTION
        )

    await db.flush()
