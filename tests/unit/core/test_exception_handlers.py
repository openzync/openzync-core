"""Unit tests for exception handler registration and RFC 7807 response integrity.

Tests validate that:
1. All registered exception handlers are async coroutines (prevents Starlette's
   ``run_in_threadpool`` race with ``ServerErrorMiddleware``).
2. ``_to_problem_json`` produces responses where ``Content-Length`` matches the
   actual body byte length.
"""

from __future__ import annotations

import inspect

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from core.exceptions import (
    _to_problem_json,
    NotFoundError,
    register_exception_handlers,
)


class TestHandlerAsyncness:
    """Every registered handler must be an async coroutine function.

    Starlette's ``wrap_app_handling_exceptions`` routes sync handlers through
    ``run_in_threadpool()``, which can race with ``ServerErrorMiddleware`` when
    the handler is called after an exception has already occurred.  All handlers
    **must** be ``async def`` so Starlette calls them directly on the event loop.
    """

    @pytest.mark.unit
    def test_all_handlers_are_async(self) -> None:
        """Verify all handlers are async — proxy test for the ``run_in_threadpool`` race.

        Starlette routes sync exception handlers through
        ``run_in_threadpool()``, which creates a race with
        ``ServerErrorMiddleware`` when the handler is invoked after an
        exception has already been raised.  Sync handlers can silently
        produce incorrect responses or hang.  Every handler **must** be
        ``async def`` so Starlette calls it directly on the event loop,
        bypassing the threadpool entirely.
        """
        app = FastAPI()
        register_exception_handlers(app)

        non_async: list[str] = []
        for exc_type, handler in app.exception_handlers.items():
            if not inspect.iscoroutinefunction(handler):
                non_async.append(
                    f"{exc_type.__name__}: {type(handler).__name__}"
                )

        assert not non_async, (
            f"All exception handlers must be async coroutines; "
            f"found sync handlers: {non_async}"
        )


class TestProblemJsonResponseIntegrity:
    """RFC 7807 Problem Details responses must have accurate Content-Length."""

    @staticmethod
    def _make_request(path: str = "/v1/projects/123/sessions/456") -> Request:
        """Build a minimal valid Starlette ``Request``."""
        scope: dict = {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 54321),
            "query_string": b"",
            "headers": [],
        }
        return Request(scope)

    @pytest.mark.unit
    def test_to_problem_json_body_length_matches_content_length(self) -> None:
        """Content-Length header must equal the actual response body length."""
        request = self._make_request()
        exc = NotFoundError("Session not found", detail={"session_id": "456"})

        response = _to_problem_json(request, exc)
        body_len = len(response.body)
        content_length = response.headers.get("content-length")

        assert content_length is not None, (
            "Content-Length header must be set"
        )
        assert int(content_length) == body_len, (
            f"Content-Length ({content_length}) != body length ({body_len}); "
            f"body was: {response.body.decode()}"
        )
