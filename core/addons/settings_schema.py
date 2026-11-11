"""Per-org addon settings schemas (``settings.schema.json``).

Bundles may ship an optional ``settings.schema.json`` beside ``addon.toml``.
It is validated once at install/upgrade time (:func:`parse_settings_schema`)
and snapshotted on the addon row; per-org values are validated against it on
every write (:func:`validate_settings_values`, jsonschema-backed).

Supported property ``type`` values: text, textarea, number, integer,
boolean, select, multiselect, secret, url, email, json, date — plus
``object``, which denotes a settings GROUP (a named section whose
``properties`` hold scalar fields only; no nesting). Root scalars stay
flat at the top level (the implicit first group); group values nest as
``{groupId: {...}}``. ``select``/``multiselect`` require a non-empty
``enum``. ``secret`` values are stored in OpenBao (never Postgres) and
masked on read (secret refs carry the ``group.key`` path).
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
_GROUP_KEYS: frozenset[str] = frozenset(
    {"type", "title", "description", "properties", "required"}
)
"""Keys a group (``type: object``) property may declare."""


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
            outside ``properties`` (root ``required`` may only name scalar
            props, never groups), unknown keys, invalid defaults, groups
            without a non-empty title/properties, or nested objects.
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
    grouped_required = [r for r in required if _is_group(props[r])]
    if grouped_required:
        raise ValidationError(f"required may not name groups: {grouped_required}.")
    for name, prop in props.items():
        if _is_group(prop):
            for key, sub in prop["properties"].items():
                if sub.get("default") is not None:
                    _check_default(f"{name}.{key}", _fragment(sub), sub["default"])
        elif prop.get("default") is not None:
            _check_default(name, _fragment(prop), prop["default"])
    return doc


def _check_default(name: str, fragment: dict[str, Any], default: Any) -> None:
    """Validate one declared default against its JSON Schema fragment.

    Args:
        name: The dotted setting name (for error messages).
        fragment: The JSON Schema fragment for the declaring property.
        default: The declared default value.

    Raises:
        ValidationError: When the default violates its own schema.
    """
    try:
        jsonschema.Draft202012Validator(fragment).validate(default)
    except jsonschema.ValidationError as exc:
        raise ValidationError(
            f"Invalid default for setting {name!r}: {exc.message}."
        ) from exc


def _is_group(prop: dict[str, Any]) -> bool:
    """Return whether a validated property declaration is a group."""
    return prop.get("type") == "object"


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
    stype = prop.get("type")
    if stype == "object":
        _check_group(name, prop)
        return
    unknown = set(prop) - _PROP_KEYS
    if unknown:
        raise ValidationError(f"Setting {name!r}: unknown keys {sorted(unknown)}.")
    if stype not in SETTING_TYPES:
        raise ValidationError(f"Setting {name!r}: unknown type {stype!r}.")
    _check_scalar_constraints(name, prop, stype)


def _check_scalar_constraints(name: str, prop: dict[str, Any], stype: str) -> None:
    """Validate enum placement for one scalar property.

    Args:
        name: The property name (may be dotted ``group.key`` for messages).
        prop: The raw property declaration.
        stype: The already-checked scalar type value.

    Raises:
        ValidationError: When ``enum`` placement is wrong.
    """
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


def _check_group(name: str, prop: dict[str, Any]) -> None:
    """Validate one group (``type: object``) declaration.

    Args:
        name: The group name.
        prop: The raw group declaration.

    Raises:
        ValidationError: On missing title/properties, bad ``required``,
            ``default``/``enum`` on the group itself, or nested objects.
    """
    unknown = set(prop) - _GROUP_KEYS
    if unknown:
        raise ValidationError(f"Setting {name!r}: unknown keys {sorted(unknown)}.")
    title = prop.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValidationError(f"Setting {name!r}: groups need a non-empty title.")
    description = prop.get("description", "")
    if not isinstance(description, str):
        raise ValidationError(f"Setting {name!r}: description must be a string.")
    inner = prop.get("properties")
    if not isinstance(inner, dict) or not inner:
        raise ValidationError(
            f"Setting {name!r}: groups need a non-empty properties object."
        )
    for key, sub in inner.items():
        _check_group_field(name, key, sub)
    required = prop.get("required", [])
    if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
        raise ValidationError(f"Setting {name!r}: required must be string list.")
    unknown_required = [r for r in required if r not in inner]
    if unknown_required:
        raise ValidationError(
            f"Setting {name!r}: required not in properties: {unknown_required}."
        )


def _check_group_field(group: str, name: str, prop: object) -> None:
    """Validate one scalar field inside a group (objects rejected).

    Args:
        group: The enclosing group name (for error messages).
        name: The field name.
        prop: The raw field declaration.

    Raises:
        ValidationError: On any structural problem.
    """
    label = f"{group}.{name}"
    if not isinstance(name, str) or not IDENT_RE.match(name):
        raise ValidationError(f"Bad setting name {label!r}: identifier-safe only.")
    if not isinstance(prop, dict):
        raise ValidationError(f"Setting {label!r} must be an object.")
    if prop.get("type") == "object":
        raise ValidationError(f"Setting {label!r}: nested objects not allowed.")
    unknown = set(prop) - _PROP_KEYS
    if unknown:
        raise ValidationError(f"Setting {label!r}: unknown keys {sorted(unknown)}.")
    stype = prop.get("type")
    if stype not in SETTING_TYPES:
        raise ValidationError(f"Setting {label!r}: unknown type {stype!r}.")
    _check_scalar_constraints(label, prop, stype)


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
    keys are rejected via ``additionalProperties: false``. Group values
    nest as ``{groupId: {...}}`` while root scalars stay flat; error
    messages pinpoint the dotted ``group.key`` path.

    Args:
        schema: A schema previously returned by :func:`parse_settings_schema`.
        values: The org-supplied values object.

    Returns:
        The validated values with defaults applied (same nesting shape).

    Raises:
        ValidationError: On non-object values or any schema violation.
    """
    if not isinstance(values, dict):
        raise ValidationError("Settings values must be an object.")
    props = schema.get("properties", {})
    filled = dict(values)
    for name, prop in props.items():
        if _is_group(prop):
            gval = filled.get(name)
            if gval is None:
                gfilled: dict[str, Any] = {
                    k: s["default"]
                    for k, s in prop["properties"].items()
                    if s.get("default") is not None
                }
                if gfilled:
                    filled[name] = gfilled
                continue
            if not isinstance(gval, dict):
                raise ValidationError(f"Setting {name!r} must be an object.")
            gfilled = dict(gval)
            for k, s in prop["properties"].items():
                if k not in gfilled and s.get("default") is not None:
                    gfilled[k] = s["default"]
            filled[name] = gfilled
        elif name not in filled and prop.get("default") is not None:
            filled[name] = prop["default"]
    jschema: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
    for name, prop in props.items():
        if _is_group(prop):
            jschema["properties"][name] = {
                "type": "object",
                "properties": {k: _fragment(s) for k, s in prop["properties"].items()},
                "required": list(prop.get("required", [])),
                "additionalProperties": False,
            }
        else:
            jschema["properties"][name] = _fragment(prop)
    jschema["required"] = [r for r in schema.get("required", []) if r in props]
    jschema["additionalProperties"] = False
    try:
        jsonschema.Draft202012Validator(jschema).validate(filled)
    except jsonschema.ValidationError as exc:
        raise ValidationError(_values_error(exc)) from exc
    return filled


def _values_error(exc: jsonschema.ValidationError) -> str:
    """Render a jsonschema failure with its dotted ``group.key`` location.

    Args:
        exc: The jsonschema validation error.

    Returns:
        A ``ValidationError``-ready message; flat schemas keep the exact
        historical shape (no location prefix when the failure is at root).
    """
    path = [str(p) for p in exc.absolute_path]
    if path and exc.validator in ("required", "additionalProperties"):
        names = re.findall(r"'([^']+)'", exc.message)
        if names:
            loc = ", ".join(f"{'.'.join([*path, n])}" for n in names)
            return f"Invalid addon settings at {loc}: {exc.message}."
    if path:
        return f"Invalid addon settings at {'.'.join(path)!r}: {exc.message}."
    return f"Invalid addon settings: {exc.message}."


def normalize_groups(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Split a parsed schema into ordered settings groups for the UI.

    Explicit ``type: object`` properties become groups in declaration
    order; when root scalars exist they form an implicit first group
    (``general``, or ``__general`` on collision).

    Args:
        schema: A schema previously returned by :func:`parse_settings_schema`.

    Returns:
        ``[{id, title, description, properties, required}, ...]`` — the
        implicit group first when present, then explicit groups.
    """
    props = schema.get("properties", {})
    groups = [
        {
            "id": name,
            "title": prop["title"],
            "description": prop.get("description", ""),
            "properties": dict(prop["properties"]),
            "required": list(prop.get("required", [])),
        }
        for name, prop in props.items()
        if _is_group(prop)
    ]
    scalars = {n: p for n, p in props.items() if not _is_group(p)}
    if scalars:
        gid = "general" if "general" not in props else "__general"
        suffix = 2
        while gid in props:
            gid = f"__general_{suffix}"
            suffix += 1
        groups.insert(
            0,
            {
                "id": gid,
                "title": schema.get("title") or "General",
                "description": schema.get("description") or "",
                "properties": scalars,
                "required": [r for r in schema.get("required", []) if r in scalars],
            },
        )
    return groups


def secret_keys(schema: dict[str, Any]) -> set[str]:
    """Return the secret setting refs (``key`` or dotted ``group.key``).

    Args:
        schema: A parsed settings schema.

    Returns:
        The secret refs; flat schemas yield plain property names.
    """
    keys = set()
    for name, prop in schema.get("properties", {}).items():
        if _is_group(prop):
            keys.update(
                f"{name}.{k}"
                for k, s in prop["properties"].items()
                if s.get("type") == "secret"
            )
        elif prop.get("type") == "secret":
            keys.add(name)
    return keys


def split_secrets(
    schema: dict[str, Any], values: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split validated values into Postgres-plain and OpenBao-secret parts.

    Both sides keep the nested shape (groups without remaining keys are
    omitted).

    Args:
        schema: A parsed settings schema.
        values: Validated settings values.

    Returns:
        ``(plain, secrets)`` — disjoint dicts covering ``values``.
    """
    keys = secret_keys(schema)
    plain: dict[str, Any] = {}
    secrets: dict[str, Any] = {}
    for name, value in values.items():
        if isinstance(value, dict) and any(k.startswith(f"{name}.") for k in keys):
            p_group, s_group = {}, {}
            for k, v in value.items():
                (s_group if f"{name}.{k}" in keys else p_group)[k] = v
            if p_group:
                plain[name] = p_group
            if s_group:
                secrets[name] = s_group
        elif name in keys:
            secrets[name] = value
        else:
            plain[name] = value
    return plain, secrets


def merge_plain_secrets(
    plain: dict[str, Any], secrets: dict[str, Any]
) -> dict[str, Any]:
    """Deep-merge (one level) Postgres-plain and OpenBao-secret parts.

    Args:
        plain: Non-secret values (nested shape).
        secrets: Secret values (nested shape).

    Returns:
        The merged values; group dicts combine key-wise.
    """
    merged = dict(plain)
    for name, value in secrets.items():
        base = merged.get(name)
        if isinstance(base, dict) and isinstance(value, dict):
            merged[name] = {**base, **value}
        else:
            merged[name] = value
    return merged


def mask_secrets(schema: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """Replace stored secret values with the mask placeholder for reads.

    Args:
        schema: A parsed settings schema.
        values: Merged plain + secret values (nested shape).

    Returns:
        A copy with every present secret value replaced by ``***``.
    """
    keys = secret_keys(schema)
    masked: dict[str, Any] = {}
    for name, value in values.items():
        if isinstance(value, dict) and any(k.startswith(f"{name}.") for k in keys):
            masked[name] = {
                k: (MASKED_SECRET if f"{name}.{k}" in keys else v)
                for k, v in value.items()
            }
        else:
            masked[name] = MASKED_SECRET if name in keys else value
    return masked
