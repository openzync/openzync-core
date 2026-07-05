"""User tool — create_user.

Provides user management operations for the OpenZync platform.
"""

from __future__ import annotations

import logging
import time

from fastmcp import Context

from services.mcp.server import mcp

logger = logging.getLogger("openzync.mcp.tools.users")


@mcp.tool
async def create_user(
    ctx: Context,
    external_id: str,
    name: str | None = None,
) -> str:
    """Create a new user in the system.

    Users represent end-users within an organization.  The ``external_id``
    is chosen by the calling application (e.g. a UUID from the customer's
    auth system).  The combination ``(organization_id, external_id)`` is
    unique within the platform.

    Args:
        external_id: Caller-defined user identifier
            (e.g. ``"customer-abc-123"``).  Must be non-empty.
        name: Optional display name for the user.

    Returns:
        A confirmation message with the created user's ID and details.
    """
    if not external_id or not external_id.strip():
        raise ValueError("external_id must be a non-empty string.")

    start = time.monotonic()
    logger.info("mcp.tool.invoke tool=%s external_id=%s name=%s",
                "create_user", external_id, name)

    client = ctx.lifespan_context["client"]
    try:
        user = await client.users.create(
            external_id=external_id,
            name=name,
        )

        elapsed = time.monotonic() - start
        logger.info("mcp.tool.success tool=%s duration_ms=%d user_id=%s external_id=%s",
                    "create_user", round(elapsed * 1000), user.id, user.external_id)

        return (
            f"User created successfully.\n"
            f"  ID: {user.id}\n"
            f"  External ID: {user.external_id}\n"
            f"  Name: {user.name}"
        )
    except Exception:
        elapsed = time.monotonic() - start
        logger.error("mcp.tool.error tool=%s duration_ms=%d external_id=%s",
                     "create_user", round(elapsed * 1000), external_id, exc_info=True)
        raise
