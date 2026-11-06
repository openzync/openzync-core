"""Per-org addon settings schemas (``settings.schema.json``).

Bundles may ship an optional ``settings.schema.json`` beside ``addon.toml``.
It is validated once at install/upgrade time (:func:`parse_settings_schema`)
and snapshotted on the addon row; per-org values are validated against it on
every write (:func:`validate_settings_values`, jsonschema-backed).

Supported property ``type`` values: text, textarea, number, integer,
boolean, select, multiselect, secret, url, email, json, date.
``select``/``multiselect`` require a non-empty ``enum``. ``secret`` values
are stored in OpenBao (never Postgres) and masked on read.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import jsonschema

from core.exceptions import ValidationError

logger = logging.getLogger(__name__)

SETTING_TYPES: frozenset[str] = frozenset(
    {
        "text",
        "textarea",
        "number",
        "integer",
        "boolean",
        "select",
        "multiselect",
        "secret",
        "url",
        "email",
        "json",
        "date",
    }
)
"""Property types a settings schema may declare."""

MASKED_SECRET = "***"  # noqa: S105 — display placeholder, not a credential.
"""Placeholder returned for secret values on read; ``PUT`` it back to keep."""

IDENT_RE: re.Pattern[str] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
"""Property names: identifier-safe (usable as OpenBao keys)."""

MAX_SCHEMA_BYTES = 256 * 1024
"""Largest accepted ``settings.schema.json`` (256 KiB) — fail-closed above."""

_ROOT_KEYS: frozenset[str] = frozenset(
    {"type", "title", "description", "properties", "required"}
)
_PROP_KEYS: frozenset[str] = frozenset(
    {
        "type",
        "title",
        "description",
        "default",
        "enum",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
    }
)


def parse_settings_schema(raw: bytes) -> dict[str, Any]:
    """Validate ``settings.schema.json`` bytes at install/upgrade time.

    Args:
        raw: The raw file bytes.

    Returns:
        The parsed schema dict (JSON-safe, snapshot-ready).

    Raises:
        ValidationError: On oversize, bad JSON, non-object root, bad
            property names/types, ``select``/``multiselect`` without a
            non-empty ``enum``, ``enum`` on other types, ``required``
            outside ``properties``, unknown keys, or invalid defaults.
    """
    if len(raw) > MAX_SCHEMA_BYTES:
        raise ValidationError(
            f"settings.schema.json too large ({len(raw)} > {MAX_SCHEMA_BYTES})."
        )
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"settings.schema.json is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValidationError("settings.schema.json root must be an object.")
    if doc.get("type") != "object":
        raise ValidationError("settings.schema.json must declare type object.")
    unknown_root = set(doc) - _ROOT_KEYS
    if unknown_root:
        raise ValidationError(f"Unknown settings schema keys: {sorted(unknown_root)}.")
    props = doc.get("properties", {})
    if not isinstance(props, dict):
        raise ValidationError("settings.schema.json properties must be an object.")
    for name, prop in props.items():
        _check_property(name, prop)
    required = doc.get("required", [])
    if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
        raise ValidationError("settings.schema.json required must be string list.")
    unknown_required = [r for r in required if r not in props]
    if unknown_required:
        raise ValidationError(f"required not in properties: {unknown_required}.")
    for name, prop in props.items():
        if "default" in prop and prop["default"] is not None:
            try:
                jsonschema.Draft202012Validator(_fragment(prop)).validate(
                    prop["default"]
                )
            except jsonschema.ValidationError as exc:
                raise ValidationError(
                    f"Invalid default for setting {name!r}: {exc.message}."
                ) from exc
    return doc


def _check_property(name: str, prop: object) -> None:
    """Validate one property declaration, fail-closed.

    Args:
        name: The property name.
        prop: The raw property declaration.

    Raises:
        ValidationError: On any structural problem.
    """
    if not isinstance(name, str) or not IDENT_RE.match(name):
        raise ValidationError(f"Bad setting name {name!r}: identifier-safe only.")
    if not isinstance(prop, dict):
        raise ValidationError(f"Setting {name!r} must be an object.")
    unknown = set(prop) - _PROP_KEYS
    if unknown:
        raise ValidationError(f"Setting {name!r}: unknown keys {sorted(unknown)}.")
    stype = prop.get("type")
    if stype not in SETTING_TYPES:
        raise ValidationError(f"Setting {name!r}: unknown type {stype!r}.")
    if stype in ("select", "multiselect"):
        enum = prop.get("enum")
        if (
            not isinstance(enum, list)
            or not enum
            or not all(isinstance(e, str) for e in enum)
        ):
            raise ValidationError(f"Setting {name!r}: {stype} needs non-empty enum.")
    elif "enum" in prop:
        raise ValidationError(f"Setting {name!r}: enum needs select/multiselect.")


def _fragment(prop: dict[str, Any]) -> dict[str, Any]:
    """Translate one property declaration to a JSON Schema fragment.

    Args:
        prop: A validated property declaration.

    Returns:
        The equivalent JSON Schema (``secret``/``url``/``email``/``date``
        validate as plain strings — no format assertion).
    """
    stype = prop["type"]
    if stype in ("text", "textarea", "secret", "url", "email", "date"):
        frag: dict[str, Any] = {"type": "string"}
    elif stype == "number":
        frag = {"type": "number"}
    elif stype == "integer":
        frag = {"type": "integer"}
    elif stype == "boolean":
        frag = {"type": "boolean"}
    elif stype == "select":
        frag = {"type": "string", "enum": prop["enum"]}
    elif stype == "multiselect":
        frag = {"type": "array", "items": {"enum": prop["enum"]}}
    else:  # json — any value accepted
        return {}
    for bound in ("minimum", "maximum", "minLength", "maxLength"):
        if bound in prop:
            frag[bound] = prop[bound]
    return frag


def validate_settings_values(
    schema: dict[str, Any], values: dict[str, Any]
) -> dict[str, Any]:
    """Validate org-supplied values against a parsed schema.

    Defaults fill missing keys before ``required`` is enforced; unknown
    keys are rejected via ``additionalProperties: false``.

    Args:
        schema: A schema previously returned by :func:`parse_settings_schema`.
        values: The org-supplied values object.

    Returns:
        The validated values with defaults applied.

    Raises:
        ValidationError: On non-object values or any schema violation.
    """
    if not isinstance(values, dict):
        raise ValidationError("Settings values must be an object.")
    props = schema.get("properties", {})
    filled = dict(values)
    for name, prop in props.items():
        if name not in filled and prop.get("default") is not None:
            filled[name] = prop["default"]
    jschema = {
        "type": "object",
        "properties": {n: _fragment(p) for n, p in props.items()},
        "required": [r for r in schema.get("required", []) if r in props],
        "additionalProperties": False,
    }
    try:
        jsonschema.Draft202012Validator(jschema).validate(filled)
    except jsonschema.ValidationError as exc:
        raise ValidationError(f"Invalid addon settings: {exc.message}.") from exc
    return filled


def secret_keys(schema: dict[str, Any]) -> set[str]:
    """Return the property names whose type is ``secret``.

    Args:
        schema: A parsed settings schema.

    Returns:
        The secret property names.
    """
    props = schema.get("properties", {})
    return {n for n, p in props.items() if p.get("type") == "secret"}


def split_secrets(
    schema: dict[str, Any], values: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split validated values into Postgres-plain and OpenBao-secret parts.

    Args:
        schema: A parsed settings schema.
        values: Validated settings values.

    Returns:
        ``(plain, secrets)`` — disjoint dicts covering ``values``.
    """
    keys = secret_keys(schema)
    return (
        {k: v for k, v in values.items() if k not in keys},
        {k: v for k, v in values.items() if k in keys},
    )


def mask_secrets(schema: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """Replace stored secret values with the mask placeholder for reads.

    Args:
        schema: A parsed settings schema.
        values: Merged plain + secret values.

    Returns:
        A copy with every present secret value replaced by ``***``.
    """
    keys = secret_keys(schema)
    return {k: (MASKED_SECRET if k in keys else v) for k, v in values.items()}
