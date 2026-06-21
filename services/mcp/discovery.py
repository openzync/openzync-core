"""Auto-discover MCP tool definitions from FastAPI route definitions.

Imports every non-admin router and iterates route metadata to generate
:class:`DiscoveredTool` definitions — each representing one HTTP endpoint.

Usage::
    from services.mcp.discovery import discover_tools

    for tool in discover_tools():
        print(tool.name, tool.method, tool.path)
"""

from __future__ import annotations

import importlib
import logging
import types
from dataclasses import dataclass, field
from typing import Any

from fastapi.routing import APIRoute

logger = logging.getLogger("openzep.mcp.discovery")


# ── Excluded router prefixes ─────────────────────────────────────────────────

EXCLUDED_PREFIXES: tuple[str, ...] = (
    "/admin",
    "/v1/admin",
)

EXCLUDED_MODULES: set[str] = {
    "routers.health",
    "routers.metrics",
    "routers.auth",
}


# ── Data types ────────────────────────────────────────────────────────────────


@dataclass
class ParamDef:
    """Definition of a single tool parameter."""

    name: str
    type_name: str  # Python type name as string (for code generation)
    required: bool = True
    default: Any = None
    description: str = ""


@dataclass
class DiscoveredTool:
    """A tool discovered from a FastAPI route definition."""

    name: str
    description: str
    method: str  # get, post, patch, delete, put
    path: str
    path_params: list[ParamDef] = field(default_factory=list)
    query_params: list[ParamDef] = field(default_factory=list)
    body_params: list[ParamDef] = field(default_factory=list)
    raw_handler: Any = None


# ── Type mapping ──────────────────────────────────────────────────────────────

_PY_TYPE_MAP: dict[type, str] = {
    str: "str",
    int: "int",
    float: "float",
    bool: "bool",
    bytes: "bytes",
    type(None): "None",
}


def _type_name(typ: type) -> str:
    """Return a Python-safe type name string for *typ*.

    Maps common stdlib types (UUID, datetime, etc.) to ``str`` since MCP
    tools receive them as JSON strings anyway.
    """
    from uuid import UUID

    from pydantic import BaseModel

    # Direct mapping
    if typ in _PY_TYPE_MAP:
        return _PY_TYPE_MAP[typ]
    # Pydantic models
    if isinstance(typ, type) and issubclass(typ, BaseModel):
        return typ.__name__
    # Common string-representable types → accept as str
    if typ in (UUID,):
        return "str"
    # Datetime types → accept as str (ISO format)
    import datetime

    if typ in (datetime.datetime, datetime.date, datetime.timedelta):
        return "str"
    # Generic types (list[str], dict[str, Any], etc.)
    origin = getattr(typ, "__origin__", None)
    if origin is not None:
        args = getattr(typ, "__args__", [])
        if origin is list:
            inner = _type_name(args[0]) if args else "str"
            return f"list[{inner}]"
        if origin is dict:
            k_t = _type_name(args[0]) if len(args) > 0 else "str"
            v_t = _type_name(args[1]) if len(args) > 1 else "str"
            return f"dict[{k_t}, {v_t}]"
        if origin is set:
            inner = _type_name(args[0]) if args else "str"
            return f"set[{inner}]"
        return "str"
    # Union types (str | None, Optional[str], etc.)
    if hasattr(typ, "__class__"):
        origin_class = getattr(typ.__class__, "__origin__", None)
        if origin_class is types.UnionType or type(typ).__name__ == "_GenericAlias":
            args = getattr(typ, "__args__", [typ])
            # Strip None from Union
            non_none = [a for a in args if a is not type(None)]
            if non_none:
                return _type_name(non_none[0])
            return "str"
    # Fallback
    name = getattr(typ, "__name__", str(typ))
    return name


def _type_annotation(typ: type) -> str:
    """Return a Python type annotation string, handling optional types.

    Example::
        _type_annotation(str | None) → "str | None"
        _type_annotation(int) → "int"
    """
    from types import UnionType

    # Check if it's a Union/Optional type
    origin = getattr(typ, "__origin__", None)
    if origin is types.UnionType or origin is type(types.UnionType):
        args = getattr(typ, "__args__", [typ])
        non_none = [a for a in args if a is not type(None)]
        has_none = any(a is type(None) for a in args)
        if non_none:
            base = _type_name(non_none[0])
            return f"{base} | None" if has_none else base
    if typ is type(None):
        return "None"
    return _type_name(typ)


# ── Path → tool name ──────────────────────────────────────────────────────────


def _path_to_tool_name(method: str, path: str, summary: str = "") -> str:
    """Convert a URL path + HTTP method to a short, readable tool name.

    Examples::
        GET    /v1/users                  → list_users
        POST   /v1/users                  → create_user
        GET    /v1/users/{user_id}         → get_user
        PATCH  /v1/users/{user_id}         → update_user
        DELETE /v1/users/{user_id}         → delete_user
        POST   /v1/users/{user_id}/memory  → add_memory
        GET    /v1/users/{user_id}/search  → search
    """
    # Strip /v1 prefix and split
    raw = path.strip("/")
    parts = raw.split("/")
    if parts and parts[0] == "v1":
        parts = parts[1:]

    # Remove path-parameter segments entirely (they add noise to names)
    parts = [p for p in parts if not (p.startswith("{") and p.endswith("}"))]

    # Determine action prefix from method
    method_lower = method.lower()
    action_map = {
        "get": "get",
        "post": "create",
        "patch": "update",
        "put": "set",
        "delete": "delete",
    }
    action = action_map.get(method_lower, method_lower)

    # If no resource parts (e.g. GET /v1/users with parts=[] after strip),
    # the action becomes the whole name
    if not parts:
        return action

    # Build name: action + resource parts
    name_parts = [action] + parts

    # Deduplicate consecutive repeats (e.g. "delete_user_user" → "delete_user")
    deduped: list[str] = []
    for p in name_parts:
        if not deduped or deduped[-1] != p:
            deduped.append(p)

    return "_".join(deduped)


# ── Parameter extraction ──────────────────────────────────────────────────────


def _extract_path_params(dependant) -> list[ParamDef]:
    """Extract path parameter definitions from a FastAPI Dependant."""
    params: list[ParamDef] = []
    for dp in dependant.path_params:
        fi = dp.field_info
        params.append(ParamDef(
            name=dp.name,
            type_name=_type_annotation(fi.annotation),
            required=True,
        ))
    return params


def _extract_query_params(dependant) -> list[ParamDef]:
    """Extract query parameter definitions from a FastAPI Dependant."""
    params: list[ParamDef] = []
    for dp in dependant.query_params:
        fi = dp.field_info
        required = True
        default = None
        if fi.default is not None:
            try:
                # PydanticUndefined marks required params
                from pydantic.fields import PydanticUndefined

                if fi.default is PydanticUndefined:
                    required = True
                else:
                    required = False
                    default = fi.default
            except ImportError:
                required = fi.default is None
        params.append(ParamDef(
            name=dp.name,
            type_name=_type_annotation(fi.annotation),
            required=required,
            default=default,
        ))
    return params


def _extract_body_params(dependant) -> list[ParamDef]:
    """Extract body (request body) parameter definitions."""
    params: list[ParamDef] = []
    for dp in dependant.body_params:
        fi = dp.field_info
        model = fi.annotation
        if model is None:
            continue
        # Check if it's a Pydantic model with fields
        if hasattr(model, "model_fields"):
            for field_name, field_info in model.model_fields.items():
                is_required = field_info.is_required() if hasattr(field_info, "is_required") else True
                default = None
                if not is_required:
                    try:
                        default = field_info.default
                    except Exception:
                        default = None
                params.append(ParamDef(
                    name=field_name,
                    type_name=_type_annotation(field_info.annotation) if field_info.annotation else "str",
                    required=is_required,
                    default=default,
                    description=field_info.description or "",
                ))
        else:
            # Plain type (dict, list, etc.)
            params.append(ParamDef(
                name=dp.name,
                type_name=_type_annotation(model),
                required=True,
            ))
    return params


# ── Router importer ───────────────────────────────────────────────────────────


def _iter_routers() -> list[tuple[str, Any]]:
    """Import all router modules from the ``routers`` package.

    Skips modules that start with ``_`` and those in ``EXCLUDED_MODULES``
    or with prefixes in ``EXCLUDED_PREFIXES``.

    Returns:
        List of ``(module_name, router)`` tuples.
    """
    import pkgutil

    import routers as routers_pkg

    results: list[tuple[str, Any]] = []

    for importer, modname, ispkg in pkgutil.iter_modules(routers_pkg.__path__):
        if modname.startswith("_"):
            continue
        full_name = f"routers.{modname}"
        if full_name in EXCLUDED_MODULES:
            continue
        try:
            module = importlib.import_module(full_name)
        except Exception as exc:
            logger.warning("Skipping %s (import error: %s)", full_name, exc)
            continue

        router = getattr(module, "router", None)
        if router is None:
            continue

        # Check prefix exclusion
        prefix = getattr(router, "prefix", "")
        if any(prefix.startswith(ex) for ex in EXCLUDED_PREFIXES):
            continue

        results.append((full_name, router))

    return results


# ── Main discovery entry point ────────────────────────────────────────────────


def discover_tools() -> list[DiscoveredTool]:
    """Discover MCP tool definitions from all FastAPI routers.

    Iterates every non-excluded router, inspects each route's metadata
    (method, path, parameters), and returns a flat list of
    :class:`DiscoveredTool` definitions.

    Returns:
        A list of :class:`DiscoveredTool` — one per HTTP endpoint.
    """
    tools: list[DiscoveredTool] = []

    for module_name, router in _iter_routers():
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue

            for method in route.methods:
                method_lower = method.lower()
                if method_lower == "head":
                    continue

                # Build tool name + description
                description = route.summary or route.description or ""
                name = _path_to_tool_name(method_lower, route.path, description)

                # Extract parameters
                dependant = getattr(route, "dependant", None)
                path_params = _extract_path_params(dependant) if dependant else []
                query_params = _extract_query_params(dependant) if dependant else []
                body_params = _extract_body_params(dependant) if dependant else []

                # Deduplicate: if a param appears in both path and query, keep only path
                path_param_names = {p.name for p in path_params}
                query_params = [p for p in query_params if p.name not in path_param_names]

                # If body has params, they should not overlap with path/query
                body_param_names = {p.name for p in body_params}
                query_params = [p for p in query_params if p.name not in body_param_names]
                path_params = [p for p in path_params if p.name not in body_param_names]

                tools.append(DiscoveredTool(
                    name=name,
                    description=description,
                    method=method_lower,
                    path=route.path,
                    path_params=path_params,
                    query_params=query_params,
                    body_params=body_params,
                    raw_handler=route.endpoint,
                ))

                logger.debug(
                    "Discovered tool: %s (%s %s)",
                    name,
                    method.upper(),
                    route.path,
                )

    # Deduplicate by (name, method) — keep the first occurrence
    seen: set[tuple[str, str]] = set()
    unique_tools: list[DiscoveredTool] = []
    for tool in tools:
        key = (tool.name, tool.method)
        if key not in seen:
            seen.add(key)
            unique_tools.append(tool)

    logger.info("Discovered %d unique MCP tools from %d routers", len(unique_tools), len(tools))
    return unique_tools
