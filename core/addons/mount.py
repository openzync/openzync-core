"""Mount helpers — ``/v1/ext/{id}`` routers plus gated ``/ui/`` panels.

JS ``fetch()``/CSS ``url()`` must propagate ``?ticket=`` via the
withTicket snippet — the HTML rewriter covers static tags only.
"""

from __future__ import annotations

import posixpath
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import structlog
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import FileResponse, Response

from core.addons.hooks import resolve_addon_router
from core.addons.registry import AddonRecord
from core.exceptions import (
    AuthenticationError,
    AuthorizationError,
    NotFoundError,
    ServiceUnavailableError,
)

logger = structlog.get_logger(__name__)

PANEL_TICKET_TTL_S = 300
PANEL_TICKET_PREFIX = "addon:panel_ticket:"


def _addon_gate(addon_id: str, generation: int):  # type: ignore[no-untyped-def]
    from dependencies.addons import require_addon_enabled

    return require_addon_enabled(addon_id, generation)


def _tracked_mounts(app: FastAPI) -> dict[str, list[object]]:
    """Registry of route objects mounted per ``/v1/ext/{id}`` prefix.

    FastAPI ≥0.163 appends a lazy ``_IncludedRouter`` wrapper (no
    ``.path``) instead of concrete routes, so ``unmount_prefix`` cannot
    find wrappers by path. Track them by identity at mount time.
    """
    tracked = getattr(app.state, "_oz_ext_mounts", None)
    if tracked is None:
        tracked = {}
        app.state._oz_ext_mounts = tracked
    return tracked


def _track_new_routes(app: FastAPI, prefix: str, before: int) -> None:
    """Record routes appended since ``before`` under ``prefix``."""
    tracked = _tracked_mounts(app).setdefault(prefix, [])
    tracked.extend(app.router.routes[before:])


def _matches(route: object, prefix: str) -> bool:
    path = getattr(route, "path", "") or ""
    return path == prefix or path.startswith(prefix + "/")


def unmount_prefix(app: FastAPI, prefix: str) -> None:
    """Remove every route/mount under ``prefix`` (placeholder swap).

    Tracked ``_IncludedRouter`` wrappers are removed by identity (they
    have no ``.path``); anything else falls back to path matching.
    Stripping the wrapper suffices — lazy resolution reads the original
    router live, so the original is left untouched (safe in-flight).
    """
    tracked = _tracked_mounts(app).pop(prefix, [])
    routes = app.router.routes
    for route in tracked:
        if route in routes:
            routes.remove(route)
    for route in [r for r in routes if _matches(r, prefix)]:
        routes.remove(route)


def mount_placeholder(app: FastAPI, addon_id: str) -> None:
    """Mount a gated placeholder for a never-enabled addon (no routes).

    The catch-all carries the same ``require_addon_enabled`` gate, so a
    never-enabled id 403s instead of falling through to Starlette 404.
    ``mount_addon`` strips it wholesale on enable/upgrade.
    """
    from dependencies.addons import require_addon_enabled

    prefix = f"/v1/ext/{addon_id}"
    unmount_prefix(app, prefix)
    router = APIRouter()
    gate = require_addon_enabled(addon_id)

    @router.api_route(
        "",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    @router.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def _ext_placeholder(path: str = "", _a: str = Depends(gate)) -> None:
        raise NotFoundError(f"Addon {addon_id!r} has no routes.")

    before = len(app.router.routes)
    app.include_router(router, prefix=prefix)
    _track_new_routes(app, prefix, before)
    logger.info("addon.placeholder_mounted", addon=addon_id)


def mount_addon(app: FastAPI, record: AddonRecord, source_path: Path) -> None:
    """Mount one addon's router and panel static dir on the app.

    Args:
        app: The FastAPI application.
        record: The loaded addon record.
        source_path: Installed source dir (for ``ui/`` static files).
    """
    from dependencies.addons import require_addon_enabled

    addon_id = record.manifest.id
    # Strip-then-mount: replaces the install-time placeholder (or a stale
    # revision's routes) wholesale, so enable/upgrade never double-mount.
    unmount_prefix(app, f"/v1/ext/{addon_id}")
    router = record.router or getattr(record.module, "router", None)
    if router is None:
        try:
            router = resolve_addon_router(record.module)
        except Exception:
            # Loud log, graceful mount: a broken factory must not take down
            # boot or turn enable() into a silent skip. enable() itself
            # already resolved loudly; this fallback is belt-and-braces.
            logger.error("addon.register_router_failed", addon=addon_id)
            router = None
    if router is not None:
        prefix = f"/v1/ext/{addon_id}"
        before = len(app.router.routes)
        app.include_router(
            router,
            prefix=prefix,
            dependencies=[Depends(require_addon_enabled(addon_id))],
        )
        _track_new_routes(app, prefix, before)
    ui_dir = source_path / "ui"
    if ui_dir.is_dir():
        before = len(app.router.routes)
        ui_router = APIRouter()
        ui_gate = require_addon_enabled(addon_id)

        @ui_router.get("/ui/{ui_path:path}", response_model=None)
        async def _serve_ui(
            ui_path: str,
            request: Request,
            ticket: str | None = None,
            _a: str = Depends(ui_gate),  # noqa: B008
        ) -> FileResponse | Response:
            await _require_panel_ticket(
                request, addon_id, ticket, ui_path or "index.html"
            )
            target = _confine_ui_path(ui_dir, ui_path or "index.html")
            if target.is_dir():
                target = target / "index.html"
            if not target.is_file():
                raise NotFoundError(f"Panel file {ui_path!r} not found.")
            if ticket and target.suffix.lower() in (".html", ".htm"):
                try:
                    rewritten = _rewrite_html_for_ticket(
                        target.read_bytes(), ticket
                    )
                    return Response(
                        content=rewritten,
                        media_type="text/html",
                        headers={"Referrer-Policy": "no-referrer"},
                    )
                except Exception as exc:
                    logger.debug(
                        "addon.ui_rewrite_failed",
                        addon=addon_id,
                        error=str(exc),
                    )
            return FileResponse(
                str(target),
                headers={
                    "Referrer-Policy": "no-referrer",
                    "X-Content-Type-Options": "nosniff",
                },
            )

        app.include_router(ui_router, prefix=f"/v1/ext/{addon_id}")
        _track_new_routes(app, f"/v1/ext/{addon_id}", before)
    logger.info("addon.mounted", addon=addon_id)


_REWRITABLE_ATTRS = frozenset({"href", "src", "action", "poster"})


def _should_rewrite_ref(ref: str) -> bool:
    """Decide whether a relative URL should carry the panel ticket.

    Args:
        ref: The raw attribute value.

    Returns:
        True for same-directory relative refs; False for absolute
        URLs (scheme or ``//``), ``#``-only fragments, empty refs,
        and refs already carrying a ``ticket`` query param.
    """
    stripped = ref.strip()
    if not stripped or stripped.startswith("#"):
        return False
    lowered = stripped.lower()
    if lowered.startswith(("http:", "https:", "//")):
        return False
    # note: any other scheme (data:, mailto:, javascript:, blob:)
    # would corrupt if suffixed — treat as absolute and skip.
    if urlsplit(stripped).scheme or urlsplit(stripped).netloc:
        return False
    query = urlsplit(stripped).query
    return not any(
        pair.split("=")[0].strip() == "ticket"
        for pair in query.split("&")
        if pair
    )


def _append_ticket(ref: str, ticket: str) -> str:
    """Append ``?ticket=`` (or ``&ticket=``) preserving any fragment.

    Args:
        ref: The original relative URL.
        ticket: The validated panel ticket for this request.

    Returns:
        The URL with the ticket query param appended.
    """
    parts = urlsplit(ref)
    if parts.query:
        query = f"{parts.query}&ticket={quote(ticket, safe='')}"
    else:
        query = f"ticket={quote(ticket, safe='')}"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query,
                       parts.fragment))


def _rewrite_srcset_first(value: str, ticket: str) -> str:
    """Append ``ticket`` to the first URL of a ``srcset`` value.

    Only the first candidate is rewritten — enough for the boot image;
    authors must withTicket any further candidates they rely on.

    Args:
        value: The raw ``srcset`` attribute value.
        ticket: The validated panel ticket for this request.

    Returns:
        The ``srcset`` value with the first URL ticketed.
    """
    parts = value.split(",")
    first = parts[0].strip()
    if first.lower().startswith("data:"):
        return value
    tokens = first.split()
    if tokens and _should_rewrite_ref(tokens[0]):
        parts[0] = f"{_append_ticket(tokens[0], ticket)}{first[len(tokens[0]):]}"
    return ",".join(parts)


class _TicketLinkRewriter(HTMLParser):
    """HTML parser that propagates ``?ticket=`` onto relative links."""

    def __init__(self, ticket: str) -> None:
        super().__init__(convert_charrefs=False)
        self._ticket = ticket
        self.parts: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.parts.append(self._build_tag(tag, attrs, close=False))

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.parts.append(self._build_tag(tag, attrs, close=True))

    def handle_endtag(self, tag: str) -> None:
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_comment(self, data: str) -> None:
        self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self.parts.append(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        self.parts.append(f"<?{data}>")

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    def handle_unknown_decl(self, data: str) -> None:
        self.parts.append(f"<![{data}]>")

    def _build_tag(
        self, tag: str, attrs: list[tuple[str, str | None]], close: bool
    ) -> str:
        """Rebuild one tag, rewriting relative link attrs in place."""
        rendered: list[str] = []
        for name, value in attrs:
            lowered = name.lower()
            if lowered == "srcset" and value:
                value = _rewrite_srcset_first(value, self._ticket)
            elif (
                value is not None
                and lowered in _REWRITABLE_ATTRS
                and _should_rewrite_ref(value)
            ):
                value = _append_ticket(value, self._ticket)
            if value is None:
                rendered.append(f" {name}")
            else:
                rendered.append(f' {name}="{escape(value, quote=True)}"')
        suffix = " />" if close else ">"
        return f"<{tag}{''.join(rendered)}{suffix}"


def _rewrite_html_for_ticket(payload: bytes, ticket: str) -> bytes:
    """Rewrite relative ``href``/``src``/``action``/``poster`` attrs.

    ``srcset`` carries only its first URL (boot image); the rest, like
    JS ``fetch()``/CSS ``url()``, need the withTicket snippet.

    Args:
        payload: The raw HTML file bytes.
        ticket: The validated panel ticket for this request.

    Returns:
        Rewritten HTML bytes (UTF-8).

    Raises:
        UnicodeDecodeError: When the payload is not UTF-8 decodable.
        Exception: On any HTML parse failure (caller serves bytes
            untouched — fail-open to today's 401 behavior).
    """
    text = payload.decode("utf-8")
    parser = _TicketLinkRewriter(ticket)
    parser.feed(text)
    parser.close()
    return "".join(parser.parts).encode("utf-8")


def _confine_ui_path(ui_dir: Path, ui_path: str) -> Path:
    """Normalize a UI sub-path and confine it under ``ui_dir``.

    Args:
        ui_dir: The addon's installed ``ui/`` directory.
        ui_path: The raw ``{ui_path:path}`` route parameter.

    Returns:
        The confined absolute target path.

    Raises:
        NotFoundError: On ``..`` escapes, backslashes, or absolute paths
            resolving outside ``ui_dir``.
    """
    if "\\" in ui_path:
        raise NotFoundError(f"Panel file {ui_path!r} not found.")
    normalized = posixpath.normpath("/" + ui_path.lstrip("/"))
    if ".." in normalized.split("/"):
        raise NotFoundError(f"Panel file {ui_path!r} not found.")
    base = ui_dir.resolve()
    target = (base / normalized.lstrip("/")).resolve()
    if target != base and base not in target.parents:
        raise NotFoundError(f"Panel file {ui_path!r} not found.")
    return target


async def _require_panel_ticket(
    request: Request, addon_id: str, ticket: str | None, ui_path: str = ""
) -> None:
    """Validate a ``?ticket=`` panel ticket for this addon.

    Tickets are issued by ``POST .../panels/ticket`` to an authenticated
    viewer and authorize the panel itself plus files under the panel's
    directory for 5 minutes (bound to the addon id; the panel path and
    viewer are recorded at issuance for audit). Anything outside that
    scope 403s. Disabled addons already 403 via the route gate.

    Args:
        request: The incoming request (redis from ``app.state``).
        addon_id: The addon owning the UI dir.
        ticket: The presented ticket value.
        ui_path: The requested UI sub-path (scope-checked, not served).

    Raises:
        AuthenticationError: When the ticket is missing, unknown, or
            expired (401, same shape as the auth middleware).
        AuthorizationError: When the ticket belongs to another addon or
            the requested file is outside the ticket's dir (403).
        ServiceUnavailableError: When Redis is unreachable (fail closed).
    """
    if not ticket:
        raise AuthenticationError("Panel ticket required. Use ?ticket=.")
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise ServiceUnavailableError("Panel ticket store is unavailable.")
    import orjson

    try:
        raw = await redis.get(f"{PANEL_TICKET_PREFIX}{ticket}")
    except Exception as exc:
        logger.error("addon.ticket_lookup_failed", error=str(exc))
        raise ServiceUnavailableError("Panel ticket store is unavailable.") from exc
    if raw is None:
        raise AuthenticationError("Panel ticket is invalid or expired.")
    try:
        payload = orjson.loads(raw if isinstance(raw, (bytes, bytearray)) else raw)
    except Exception as exc:
        raise AuthenticationError("Panel ticket is invalid or expired.") from exc
    if payload.get("addon_id") != addon_id:
        raise AuthorizationError("Panel ticket is not valid for this addon.")
    allowed_dir = payload.get("allowed_dir")
    if allowed_dir != "/ui" and not (
        isinstance(allowed_dir, str) and allowed_dir.startswith("/ui/")
    ):
        raise AuthorizationError("Panel ticket is not valid for this file.")
    requested = posixpath.normpath("/ui/" + ui_path.lstrip("/"))
    if requested != "/ui" and not requested.startswith("/ui/"):
        return  # Escapes /ui/ — _confine_ui_path 404s it, as before.
    if (
        requested != payload.get("panel_path")
        and requested != allowed_dir
        and not requested.startswith(allowed_dir + "/")
    ):
        logger.warning(
            "addon.ticket_scope_denied",
            addon=addon_id,
            viewer=payload.get("viewer"),
            requested=requested,
        )
        raise AuthorizationError("Panel ticket is not valid for this file.")


def mount_all(
    app: FastAPI, records: dict[str, AddonRecord], paths: dict[str, Path]
) -> None:
    """Mount every enabled addon record (routers + panels)."""
    for addon_id, record in records.items():
        if record.enabled:
            mount_addon(app, record, paths[addon_id])


__all__ = [
    "PANEL_TICKET_PREFIX",
    "PANEL_TICKET_TTL_S",
    "mount_addon",
    "mount_all",
    "mount_placeholder",
    "unmount_prefix",
]
