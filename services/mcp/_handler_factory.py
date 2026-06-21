"""Tool handler factory — generates handlers using Pydantic models.

FastMCP **does not** support ``**kwargs`` or forward references to local
types in tool functions.  We use a controlled ``exec()`` that references
model classes stored at **module level** so Pydantic's type resolver can
find them during ``get_function_type_hints()``.

Safety: the code template is fixed; only parameter names and model names
are interpolated from our own :class:`DiscoveredTool` dataclass.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from pydantic import BaseModel, Field, create_model

from services.mcp.discovery import DiscoveredTool, ParamDef

logger = logging.getLogger("openzep.mcp.handler_factory")


# ── Module-level model registry ──────────────────────────────────────────────
# Each dynamically-created input model is stored here under a unique key so
# that Pydantic / FastMCP can resolve it during ``get_function_type_hints()``.

_model_registry: dict[str, type[BaseModel]] = {}
_model_counter: int = 0


def _register_model(model: type[BaseModel], tool_name: str) -> str:
    """Store a model at module scope and return its attribute name."""
    global _model_counter  # noqa: PLW0603
    _model_counter += 1
    # Sanitise the tool name for use as a Python identifier
    safe_name = f"_{_model_counter}_{tool_name.replace('-', '_').replace(' ', '_')}"
    _model_registry[safe_name] = model
    # Inject into module globals so exec'd code and Pydantic can find it
    globals()[safe_name] = model
    return safe_name


# ── Type resolution ───────────────────────────────────────────────────────────


def _resolve_type(type_name: str) -> type:
    """Map a type name string to a Python type for Pydantic schema generation."""
    type_map: dict[str, type] = {
        "str": str,
        "int": int,
        "float": float,
        "bool": bool,
        "bytes": str,
    }
    base = type_name.split(" |")[0].strip()
    if base.startswith("list[") or base.startswith("dict[") or base.startswith("set["):
        return str
    return type_map.get(base, str)


def _make_optional(t: type) -> type:
    """Wrap a type in Optional (type | None) if not already optional."""
    from types import UnionType

    origin = getattr(t, "__origin__", None)
    if origin is UnionType or origin is type(UnionType):
        return t
    if hasattr(t, "__args__"):
        args = getattr(t, "__args__", ())
        if type(None) in args:
            return t
    return t | None


# ── HTTP dispatcher ───────────────────────────────────────────────────────────


async def _dispatch(
    method: str,
    path: str,
    api_base: str,
    api_key: str,
    params: dict[str, Any],
) -> str:
    """Generic HTTP dispatcher — all tool handlers delegate to this."""
    headers = {"Authorization": f"Bearer {api_key}"}
    url = path
    remaining: dict[str, Any] = {}

    for k, v in params.items():
        token = "{" + k + "}"
        if token in url:
            url = url.replace(token, str(v))
        else:
            remaining[k] = v

    async with httpx.AsyncClient(base_url=api_base, headers=headers, timeout=30) as client:
        if method in ("post", "put", "patch"):
            resp = await client.request(method.upper(), url, json=remaining)
        else:
            resp = await client.request(method.upper(), url, params=remaining)

        resp.raise_for_status()
        return resp.text


# ── Input model builder ──────────────────────────────────────────────────────


def _build_input_model(tool: DiscoveredTool) -> type[BaseModel]:
    """Dynamically create a Pydantic input model for a tool."""
    seen: set[str] = set()
    fields: dict[str, tuple[type, Any]] = {}

    for group in (tool.path_params, tool.query_params, tool.body_params):
        for p in group:
            if p.name in seen:
                continue
            seen.add(p.name)

            py_type = _resolve_type(p.type_name)
            kwargs: dict[str, Any] = {"description": p.description or p.name}

            if p.required:
                kwargs["default"] = ...
            else:
                py_type = _make_optional(py_type)
                kwargs["default"] = None if p.default is None else p.default

            fields[p.name] = (py_type, Field(**kwargs))

    model_name = f"{tool.name}_input".replace("-", "_").replace(" ", "_")
    return create_model(model_name, **fields)


# ── Handler factory ──────────────────────────────────────────────────────────


def make_handler(
    tool: DiscoveredTool,
    api_base: str,
    api_key: str,
):
    """Create an async handler function for a discovered tool.

    The handler accepts a single Pydantic model parameter (``params``)
    containing all of the tool's parameters.  FastMCP auto-generates a
    JSON Schema from the model.

    Args:
        tool: Discovered tool definition from the router.
        api_base: Base URL for the REST API.
        api_key: API key for ``Authorization`` header.

    Returns:
        An async function suitable for ``FastMCP.add_tool()``.
    """
    InputModel = _build_input_model(tool)
    model_attr = _register_model(InputModel, tool.name)

    # Build the handler function via exec so the model reference in the
    # type annotation resolves against module globals.
    func_source = (
        f"async def handler(params: {model_attr}) -> str:\n"
        f'    """{tool.description.replace(chr(34), chr(39))}"""\n'
        f"    _data = params.model_dump(exclude_none=True)\n"
        f"    return await _dispatch(\n"
        f'        method="{tool.method}",\n'
        f'        path="{tool.path}",\n'
        f"        api_base=api_base,\n"
        f"        api_key=api_key,\n"
        f"        params=_data,\n"
        f"    )\n"
    )

    namespace: dict[str, Any] = {
        "_dispatch": _dispatch,
        "api_base": api_base,
        "api_key": api_key,
        model_attr: InputModel,  # so the function's globals can resolve its own type
    }
    exec(compile(func_source, f"<{tool.name}>", "exec"), namespace)
    handler = namespace["handler"]
    handler.__name__ = tool.name
    handler.__qualname__ = tool.name
    handler.__doc__ = tool.description
    return handler
