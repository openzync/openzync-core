"""Unit tests for the null-origin CORS shim (middleware/ext_null_origin_cors.py).

Drives the middleware directly with crafted ASGI scopes — no server, no
network. Covers: preflight on /v1/ext/* (200 + ACAO null + Vary merge),
passthrough elsewhere, evil-origin fall-through.
"""

from __future__ import annotations

from typing import Any

from middleware.ext_null_origin_cors import ExtNullOriginCorsMiddleware


def _scope(
    method: str, path: str, headers: list[tuple[bytes, bytes]]
) -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers,
    }


async def _receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b""}


class _Harness:
    """Capture sends; optionally record whether the inner app ran."""

    def __init__(self, inner_headers: list[tuple[bytes, bytes]] | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.inner_called = False
        self._inner_headers = inner_headers or [(b"content-type", b"text/plain")]

    async def inner(self, scope: dict, receive: Any, send: Any) -> None:
        self.inner_called = True
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": list(self._inner_headers),
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)


def _headers(messages: list[dict[str, Any]]) -> dict[bytes, bytes]:
    start = next(m for m in messages if m["type"] == "http.response.start")
    return {k.lower(): v for k, v in start["headers"]}


class TestPreflight:
    async def test_null_origin_preflight_200_acao_null(self) -> None:
        harness = _Harness()
        middleware = ExtNullOriginCorsMiddleware(harness.inner)
        await middleware(
            _scope(
                "OPTIONS",
                "/v1/ext/democrm/ping",
                [(b"origin", b"null"), (b"access-control-request-method", b"GET")],
            ),
            _receive,
            harness.send,
        )
        assert not harness.inner_called
        assert harness.sent[0]["status"] == 200
        headers = _headers(harness.sent)
        assert headers[b"access-control-allow-origin"] == b"null"
        assert headers[b"vary"] == b"Origin"
        assert b"access-control-allow-methods" in headers
        assert b"access-control-max-age" in headers

    async def test_preflight_off_prefix_falls_through(self) -> None:
        harness = _Harness()
        middleware = ExtNullOriginCorsMiddleware(harness.inner)
        await middleware(
            _scope(
                "OPTIONS",
                "/v1/projects",
                [(b"origin", b"null"), (b"access-control-request-method", b"GET")],
            ),
            _receive,
            harness.send,
        )
        assert harness.inner_called
        assert b"access-control-allow-origin" not in _headers(harness.sent)

    async def test_preflight_evil_origin_falls_through(self) -> None:
        harness = _Harness()
        middleware = ExtNullOriginCorsMiddleware(harness.inner)
        await middleware(
            _scope(
                "OPTIONS",
                "/v1/ext/democrm/ping",
                [
                    (b"origin", b"https://evil.example"),
                    (b"access-control-request-method", b"GET"),
                ],
            ),
            _receive,
            harness.send,
        )
        assert harness.inner_called
        assert b"access-control-allow-origin" not in _headers(harness.sent)


class TestSimpleRequest:
    async def test_null_origin_get_gets_acao_null(self) -> None:
        harness = _Harness()
        middleware = ExtNullOriginCorsMiddleware(harness.inner)
        await middleware(
            _scope("GET", "/v1/ext/democrm/ping", [(b"origin", b"null")]),
            _receive,
            harness.send,
        )
        assert harness.inner_called
        assert _headers(harness.sent)[b"access-control-allow-origin"] == b"null"

    async def test_vary_merged_not_duplicated(self) -> None:
        harness = _Harness(inner_headers=[(b"vary", b"Accept")])
        middleware = ExtNullOriginCorsMiddleware(harness.inner)
        await middleware(
            _scope("GET", "/v1/ext/democrm/ping", [(b"origin", b"null")]),
            _receive,
            harness.send,
        )
        vary = _headers(harness.sent)[b"vary"]
        assert b"Origin" in vary and b"Accept" in vary

    async def test_vary_not_doubled_when_present(self) -> None:
        harness = _Harness(inner_headers=[(b"vary", b"Origin")])
        middleware = ExtNullOriginCorsMiddleware(harness.inner)
        await middleware(
            _scope("GET", "/v1/ext/democrm/ping", [(b"origin", b"null")]),
            _receive,
            harness.send,
        )
        vary = _headers(harness.sent)[b"vary"]
        assert vary.lower().split(b",").count(b"origin") == 1

    async def test_existing_acao_preserved(self) -> None:
        harness = _Harness(
            inner_headers=[(b"access-control-allow-origin", b"https://app.example")]
        )
        middleware = ExtNullOriginCorsMiddleware(harness.inner)
        await middleware(
            _scope("GET", "/v1/ext/democrm/ping", [(b"origin", b"null")]),
            _receive,
            harness.send,
        )
        headers = _headers(harness.sent)
        assert headers[b"access-control-allow-origin"] == b"https://app.example"

    async def test_other_paths_untouched(self) -> None:
        harness = _Harness()
        middleware = ExtNullOriginCorsMiddleware(harness.inner)
        await middleware(
            _scope("GET", "/v1/projects", [(b"origin", b"null")]),
            _receive,
            harness.send,
        )
        assert _headers(harness.sent) == {b"content-type": b"text/plain"}

    async def test_evil_origin_untouched(self) -> None:
        harness = _Harness()
        middleware = ExtNullOriginCorsMiddleware(harness.inner)
        await middleware(
            _scope("GET", "/v1/ext/democrm/ping", [(b"origin", b"https://evil.example")]),
            _receive,
            harness.send,
        )
        assert b"access-control-allow-origin" not in _headers(harness.sent)

    async def test_non_http_scope_passthrough(self) -> None:
        harness = _Harness()
        middleware = ExtNullOriginCorsMiddleware(harness.inner)
        await middleware({"type": "websocket", "path": "/v1/ext/x"}, _receive, harness.send)
        assert harness.inner_called
