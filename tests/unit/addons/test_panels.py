"""Unit tests for panel ticket scope + the HTML ticket rewriter.

Covers ``_require_panel_ticket`` decisions (exact/under/outside, legacy
scopeless fail-closed, the ``/ui`` root regression) and the
href/src/action/poster/first-srcset rewriting rules in
core/addons/mount.py. Redis is faked; no DB, no network.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import orjson
import pytest

from core.addons.mount import (
    _append_ticket,
    _rewrite_html_for_ticket,
    _rewrite_srcset_first,
    _require_panel_ticket,
    _should_rewrite_ref,
)
from core.exceptions import (
    AuthenticationError,
    AuthorizationError,
    ServiceUnavailableError,
)

ADDON_ID = "democrm"
TICKET = "t" * 32


def _payload(**overrides: Any) -> bytes:
    base: dict[str, Any] = {
        "addon_id": ADDON_ID,
        "panel_path": "/ui/board.html",
        "allowed_dir": "/ui",
        "viewer": "user-1",
    }
    base.update(overrides)
    return orjson.dumps(base)


class _FakeRedis:
    def __init__(self, values: dict[str, bytes] | Exception) -> None:
        self._values = values

    async def get(self, key: str) -> bytes | None:
        if isinstance(self._values, Exception):
            raise self._values
        return self._values.get(key.removeprefix("addon:panel_ticket:"))


def _request(redis: Any) -> Any:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=redis)))


class TestRequirePanelTicket:
    async def test_missing_ticket_401(self) -> None:
        with pytest.raises(AuthenticationError, match="[Tt]icket"):
            await _require_panel_ticket(_request(None), ADDON_ID, None)

    async def test_unknown_ticket_401(self) -> None:
        with pytest.raises(AuthenticationError, match="invalid or expired"):
            await _require_panel_ticket(_request(_FakeRedis({})), ADDON_ID, TICKET)

    async def test_no_redis_503(self) -> None:
        with pytest.raises(ServiceUnavailableError):
            await _require_panel_ticket(_request(None), ADDON_ID, TICKET)

    async def test_redis_failure_503(self) -> None:
        with pytest.raises(ServiceUnavailableError):
            await _require_panel_ticket(
                _request(_FakeRedis(ConnectionError("down"))), ADDON_ID, TICKET
            )

    async def test_wrong_addon_403(self) -> None:
        redis = _FakeRedis({TICKET: _payload(addon_id="other")})
        with pytest.raises(AuthorizationError):
            await _require_panel_ticket(_request(redis), ADDON_ID, TICKET, "board.html")

    async def test_legacy_scopeless_ticket_fail_closed(self) -> None:
        redis = _FakeRedis({TICKET: orjson.dumps({"addon_id": ADDON_ID})})
        with pytest.raises(AuthorizationError):
            await _require_panel_ticket(_request(redis), ADDON_ID, TICKET, "board.html")

    async def test_allowed_dir_outside_ui_rejected(self) -> None:
        redis = _FakeRedis({TICKET: _payload(allowed_dir="/evil")})
        with pytest.raises(AuthorizationError):
            await _require_panel_ticket(_request(redis), ADDON_ID, TICKET, "board.html")

    async def test_exact_panel_path_allowed(self) -> None:
        redis = _FakeRedis({TICKET: _payload()})
        await _require_panel_ticket(_request(redis), ADDON_ID, TICKET, "board.html")

    async def test_file_under_allowed_dir_allowed(self) -> None:
        redis = _FakeRedis({TICKET: _payload()})
        await _require_panel_ticket(_request(redis), ADDON_ID, TICKET, "logo.png")

    async def test_ui_root_case_scopes_whole_tree(self) -> None:
        # Regression: a ticket for /ui/index.html derives allowed_dir "/ui"
        # (dirname), so sibling files under /ui/ are in scope.
        import posixpath

        assert posixpath.dirname("/ui/index.html") == "/ui"
        redis = _FakeRedis(
            {TICKET: _payload(panel_path="/ui/index.html", allowed_dir="/ui")}
        )
        await _require_panel_ticket(_request(redis), ADDON_ID, TICKET, "index.html")
        await _require_panel_ticket(_request(redis), ADDON_ID, TICKET, "other.html")

    async def test_subdir_scope_exact_and_under(self) -> None:
        redis = _FakeRedis(
            {
                TICKET: _payload(
                    panel_path="/ui/sub/detail.html", allowed_dir="/ui/sub"
                )
            }
        )
        await _require_panel_ticket(_request(redis), ADDON_ID, TICKET, "sub/detail.html")
        await _require_panel_ticket(_request(redis), ADDON_ID, TICKET, "sub/extra.html")

    async def test_cross_panel_403(self) -> None:
        redis = _FakeRedis(
            {
                TICKET: _payload(
                    panel_path="/ui/sub/detail.html", allowed_dir="/ui/sub"
                )
            }
        )
        with pytest.raises(AuthorizationError):
            await _require_panel_ticket(_request(redis), ADDON_ID, TICKET, "board.html")

    async def test_garbage_payload_401(self) -> None:
        redis = _FakeRedis({TICKET: b"not-json{"})
        with pytest.raises(AuthenticationError):
            await _require_panel_ticket(_request(redis), ADDON_ID, TICKET, "board.html")


class TestShouldRewriteRef:
    @pytest.mark.parametrize(
        "ref",
        [
            "other.html",
            "sub/page.html",
            "logo.png",
            "/v1/ext/democrm/ping",
            "page.html?x=1",
            "page.html#sec",
            "styles.css",
        ],
    )
    def test_rewritable(self, ref: str) -> None:
        assert _should_rewrite_ref(ref) is True

    @pytest.mark.parametrize(
        "ref",
        [
            "",
            "   ",
            "#frag",
            "https://cdn.example/x.js",
            "http://x/y",
            "//cdn.example/x.js",
            "data:image/png;base64,AAA",
            "mailto:a@b.c",
            "javascript:void(0)",
            "blob:abc",
            "page.html?ticket=OLD",
            "page.html?a=1&ticket=OLD",
        ],
    )
    def test_skipped(self, ref: str) -> None:
        assert _should_rewrite_ref(ref) is False


class TestAppendTicket:
    def test_simple(self) -> None:
        assert _append_ticket("a.html", TICKET) == f"a.html?ticket={TICKET}"

    def test_query_merge(self) -> None:
        assert _append_ticket("a.html?x=1", TICKET) == f"a.html?x=1&ticket={TICKET}"

    def test_fragment_preserved(self) -> None:
        assert _append_ticket("a.html#sec", TICKET) == f"a.html?ticket={TICKET}#sec"
        assert _append_ticket("a.html?x=1#s", "a b") == "a.html?x=1&ticket=a%20b#s"


class TestRewriteSrcset:
    def test_first_url_rewritten_rest_untouched(self) -> None:
        out = _rewrite_srcset_first("a.png 1x, b.png 2x", TICKET)
        assert out.startswith(f"a.png?ticket={TICKET} 1x")
        assert "b.png?ticket" not in out

    def test_data_url_untouched(self) -> None:
        value = "data:image/png;base64,AAA 1x, b.png 2x"
        assert _rewrite_srcset_first(value, TICKET) == value


class TestRewriteHtml:
    def test_rewrites_link_attrs(self) -> None:
        html = (
            '<html><head><link href="styles.css"></head>'
            '<body><a href="other.html">x</a>'
            '<img src="logo.png"><form action="submit">'
            '<input type="image" src="go.png">'
            '<object data="movie.swf"></object>'
            '<embed poster="p.png" src="v.mp4"></body></html>'
        )
        out = _rewrite_html_for_ticket(html.encode(), TICKET).decode()
        assert f'href="other.html?ticket={TICKET}"' in out
        assert f'href="styles.css?ticket={TICKET}"' in out
        assert f'src="logo.png?ticket={TICKET}"' in out
        assert f'action="submit?ticket={TICKET}"' in out
        assert f'poster="p.png?ticket={TICKET}"' in out
        # `data=` is not a rewritable attr — left alone.
        assert 'data="movie.swf"' in out

    def test_skips_absolute_and_ticketed(self) -> None:
        html = (
            '<a href="https://x.example/a">a</a>'
            '<a href="#frag">b</a>'
            '<img src="data:image/png;base64,AAA">'
            f'<a href="b.html?ticket={TICKET}">c</a>'
        )
        out = _rewrite_html_for_ticket(html.encode(), TICKET).decode()
        assert 'href="https://x.example/a"' in out
        assert 'href="#frag"' in out
        assert 'src="data:image/png;base64,AAA"' in out
        assert out.count("ticket=") == 1

    def test_srcset_first_only(self) -> None:
        html = f'<img srcset="a.png 1x, b.png 2x" src="a.png">'
        out = _rewrite_html_for_ticket(html.encode(), TICKET).decode()
        assert f"a.png?ticket={TICKET}" in out
        assert "b.png?ticket" not in out

    def test_non_html_bytes_fail_open_with_decode_error(self) -> None:
        # Callers catch this and serve the bytes untouched (fail-open).
        with pytest.raises(UnicodeDecodeError):
            _rewrite_html_for_ticket(b"\xff\xfe\x00binary", TICKET)

    def test_plain_text_untouched(self) -> None:
        payload = b'{"status": "ok"}'
        assert _rewrite_html_for_ticket(payload, TICKET) == payload
