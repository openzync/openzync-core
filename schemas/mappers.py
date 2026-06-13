"""ORM-to-dict mapper helpers for session and episode models.

These helpers bridge the ``metadata_`` → ``metadata`` naming convention:
SQLAlchemy reserves ``metadata`` for its own ``DeclarativeBase``, so the
ORM attribute is ``metadata_``.  The Pydantic schemas use ``metadata``.
These functions convert ORM objects to plain dicts with the correct key names.

Placed in the schemas layer because they shape ORM data into the exact
structure expected by Pydantic response schemas — a data-shaping concern,
not a data-access concern.  The service layer calls these to prepare
ORM objects for ``model_validate``.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from models.episode import Episode
from models.session import Session


def session_to_dict(
    session: Session,
    *,
    message_count: int = 0,
    fact_count: int = 0,
) -> dict[str, Any]:
    """Convert a Session ORM model to a flat dict for schema construction.

    Handles the ``metadata_`` → ``metadata`` field-name mapping.
    """
    return {
        "id": session.id,
        "user_id": session.user_id,
        "external_id": session.external_id,
        "metadata": session.metadata_ or {},
        "is_active": session.is_active,
        "closed_at": session.closed_at,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "message_count": message_count,
        "fact_count": fact_count,
    }


def session_to_list_dict(
    session: Session,
    *,
    message_count: int = 0,
    fact_count: int = 0,
) -> dict[str, Any]:
    """Convert a Session ORM model to a lightweight list-item dict.

    Matches the ``SessionListResponse`` schema (excludes metadata and
    updated_at for compact list responses).
    """
    return {
        "id": session.id,
        "user_id": session.user_id,
        "external_id": session.external_id,
        "is_active": session.is_active,
        "created_at": session.created_at,
        "message_count": message_count,
        "fact_count": fact_count,
    }


def episode_to_dict(episode: Episode) -> dict[str, Any]:
    """Convert an Episode ORM model to a flat dict for MessageResponse.

    Handles the ``metadata_`` → ``metadata`` field-name mapping for
    episode messages.
    """
    return {
        "id": episode.id,
        "role": episode.role,
        "content": episode.content,
        "metadata": episode.metadata_ or {},
        "token_count": episode.token_count or 0,
        "sequence_number": episode.sequence_number,
        "created_at": episode.created_at,
    }
