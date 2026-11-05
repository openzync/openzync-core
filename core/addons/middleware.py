"""Addon slot middleware — one stable entry that dispatches to addons.

The middleware stack is fixed at app creation, so hot-loaded addons cannot
``add_middleware`` later. Instead a single :class:`AddonMiddleware` is
registered **inside Auth** (see ``services/api/main.py`` — it sees
``request.state.org_id``) and forwards each request through every enabled
addon's registered middlewares at request time. Loading or disabling an
addon takes effect on the next request with no restart.
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)


class AddonMiddleware:
    """Dispatching slot for addon middlewares (registered inside Auth).

    Each addon middleware is an ASGI factory ``(app) -> asgi_app`` or a
    plain ASGI app; both shapes are accepted.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Store the downstream ASGI app.

        Args:
            app: The next app in the middleware chain.
        """
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Run enabled addon middlewares outside-in, then downstream.

        Args:
            scope: The ASGI connection scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.
        """
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return

        request_app = scope.get("app")
        states = getattr(request_app.state, "addons", {}) if request_app else {}
        addons = list(states.values())
        chain: ASGIApp = self._app
        for record in reversed(addons):
            if not record.enabled:
                continue
            for factory in reversed(record.middlewares):
                # One bad factory degrades its own addon, never 500s traffic.
                try:
                    chain = self._wrap(factory, chain)
                except Exception:
                    logger.exception(
                        "addon.middleware_degraded",
                        extra={"addon_id": record.id},
                    )
                    break
        await chain(scope, receive, send)

    def _wrap(self, factory: Any, downstream: ASGIApp) -> ASGIApp:
        """Adapt one addon middleware factory to a plain ASGI app.

        Args:
            factory: An ``(app) -> asgi`` factory.
            downstream: The next app in the chain.

        Returns:
            The composed ASGI app.
        """
        return factory(downstream)
