"""Verify all 204 endpoints return ``Response`` rather than ``None``.

Returning ``None`` with ``status_code=204`` causes FastAPI to wrap it in
``JSONResponse(None)`` → ``body=b'null'`` (4 bytes).  Since
``Content-Length`` is skipped for 204 by Starlette, uvicorn expects 0 bytes
but gets 4 → ``RuntimeError``: Response content longer than Content-Length.

All 204 endpoints **must** return ``Response(status_code=204)`` explicitly
so the body is empty (``b""``) and uvicorn's check passes.

This test enforces that the return type annotation of every 204 endpoint
is ``starlette.responses.Response``, not ``None``.
"""

from __future__ import annotations

import importlib
import typing

import pytest
from starlette.responses import Response


@pytest.mark.unit
@pytest.mark.parametrize(
    ("function_path", "module_path", "function_name"),
    [
        (
            "routers/sessions.py",
            "routers.sessions",
            "delete_session",
        ),
        (
            "routers/users.py",
            "routers.users",
            "delete_user",
        ),
        (
            "routers/users.py",
            "routers.users",
            "delete_user_summary_instructions",
        ),
        (
            "routers/admin_organizations.py",
            "routers.admin_organizations",
            "delete_prompt_template_override",
        ),
        (
            "routers/admin_webhooks.py",
            "routers.admin_webhooks",
            "delete_webhook",
        ),
        (
            "routers/graph.py",
            "routers.graph",
            "delete_graph_node",
        ),
    ],
)
def test_204_endpoint_returns_response(
    function_path: str,
    module_path: str,
    function_name: str,
) -> None:
    """204 endpoint must return ``Response``, not ``None`` (no JSONResponse wrapping)."""
    mod = importlib.import_module(module_path)
    func = getattr(mod, function_name)

    hints = typing.get_type_hints(func)
    return_type = hints.get("return")

    assert return_type is not None, (
        f"{function_path}:{function_name} must have a return type annotation"
    )
    assert return_type is Response, (
        f"{function_path}:{function_name} returns {return_type}, "
        f"expected starlette.responses.Response"
    )
