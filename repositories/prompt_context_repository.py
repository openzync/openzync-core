"""Prompt-context repository — DB access for worker prompt auto-injection.

All SQL backing ``services/worker/prompt_renderer.py`` providers lives here.
Providers take an ``AsyncSession`` (injected type only) and delegate every
query to this repository — no ``select()``/``text()``/``db.execute()`` in
the service/worker layer.

Row-to-dict shaping for the structured-schema list happens here (same
convention as ``EpisodeRepository.search_by_bm25``); chronological ordering
and provider payload keys stay in the providers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.episode import Episode

# ╠ This file contains NO business logic — only query construction.


class PromptContextRepository:
    """All database access for prompt-context auto-injection.

    Every method accepts tenant scoping IDs; RLS is bound by the caller
    (``render_prompt``) before any provider runs.

    Args:
        db: An async SQLAlchemy session.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_episode_content(self, episode_id: UUID, org_id: UUID) -> str | None:
        """Fetch the ``content`` column of one episode.

        Args:
            episode_id: The episode being enriched.
            org_id: Tenant scope — defense-in-depth alongside RLS.

        Returns:
            The content, or ``None`` when the episode is not found.
        """
        result = await self._db.execute(
            select(Episode.content).where(
                Episode.id == episode_id,
                Episode.organization_id == org_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_episode_metadata(
        self, episode_id: UUID, org_id: UUID
    ) -> dict[str, Any] | None:
        """Fetch the ``metadata`` JSONB of one episode.

        Args:
            episode_id: The episode being enriched.
            org_id: Tenant scope — defense-in-depth alongside RLS.

        Returns:
            The metadata dict, or ``None`` when the episode is not found.
        """
        result = await self._db.execute(
            select(Episode.metadata_).where(
                Episode.id == episode_id,
                Episode.organization_id == org_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_episode_content_and_project(
        self, episode_id: UUID, org_id: UUID
    ) -> tuple[str, UUID] | None:
        """Fetch one episode's content plus its project (BM25 query source).

        Args:
            episode_id: The episode being enriched.
            org_id: Tenant scope — defense-in-depth alongside RLS.

        Returns:
            ``(content, project_id)``, or ``None`` when not found.
        """
        result = await self._db.execute(
            select(Episode.content, Episode.project_id).where(
                Episode.id == episode_id,
                Episode.organization_id == org_id,
            )
        )
        row = result.one_or_none()
        if row is None:
            return None
        return (row[0], row[1])

    async def list_session_recent_history(
        self, session_id: UUID, exclude_episode_id: UUID | None, limit: int
    ) -> list[tuple[str, str]]:
        """Fetch recent ``(role, content)`` turns for a session, newest first.

        Column-only select — detached scalars, safe past session close.

        Args:
            session_id: The session scope.
            exclude_episode_id: Episode under enrichment (excluded).
            limit: Maximum rows (newest-first; caller reverses).

        Returns:
            ``(role, content)`` rows, newest first.
        """
        query = (
            select(Episode.role, Episode.content)
            .where(
                Episode.session_id == session_id,
                Episode.is_deleted == False,  # noqa: E712 — SQLAlchemy boolean
            )
            .order_by(Episode.created_at.desc())
            .limit(limit)
        )
        if exclude_episode_id is not None:
            query = query.where(Episode.id != exclude_episode_id)
        result = await self._db.execute(query)
        return [(row[0], row[1]) for row in result.all()]

    async def list_schema_json_by_type(
        self, org_id: UUID, schema_type: str
    ) -> list[Any]:
        """Fetch active ``json_schema`` payloads for one schema type.

        Args:
            org_id: Tenant scope.
            schema_type: ``entity_type`` or ``classification``.

        Returns:
            Raw ``json_schema`` values (dicts) for active schemas.
        """
        result = await self._db.execute(
            text("""
                SELECT json_schema FROM extraction_schemas
                WHERE organization_id = :org_id
                  AND type = :schema_type
                  AND is_active = true
            """),
            {"org_id": org_id, "schema_type": schema_type},
        )
        return [row[0] for row in result.all()]

    async def list_structured_schemas(self, org_id: UUID) -> list[dict[str, Any]]:
        """Fetch active structured extraction schemas for an org.

        Args:
            org_id: Tenant scope.

        Returns:
            Dicts with ``id``, ``name``, ``json_schema``, ``prompt_template``.
        """
        result = await self._db.execute(
            text("""
                SELECT id, name, json_schema, prompt_template FROM extraction_schemas
                WHERE organization_id = :org_id
                  AND type = 'structured'
                  AND is_active = true
                ORDER BY name
            """),
            {"org_id": org_id},
        )
        return [
            {
                "id": str(row[0]),
                "name": row[1],
                "json_schema": row[2],
                "prompt_template": row[3],
            }
            for row in result.all()
        ]

    async def list_user_episodes(
        self, user_id: UUID, org_id: UUID, limit: int
    ) -> list[tuple[str, str]]:
        """Fetch recent ``(role, content)`` turns across a user's sessions.

        Args:
            user_id: The user scope.
            org_id: Tenant scope.
            limit: Maximum rows (newest-first; caller reverses).

        Returns:
            ``(role, content)`` rows, newest first.
        """
        result = await self._db.execute(
            text("""
                SELECT role, content FROM episodes
                WHERE session_id IN (
                    SELECT id FROM sessions
                    WHERE user_id = :user_id AND organization_id = :org_id
                )
                AND is_deleted = false
                ORDER BY created_at DESC
                LIMIT :limit
            """),
            {"user_id": user_id, "org_id": org_id, "limit": limit},
        )
        return [(row[0], row[1]) for row in result.fetchall()]

    async def list_user_facts(
        self,
        user_id: UUID,
        org_id: UUID,
        effective_at: datetime,
        limit: int,
    ) -> list[tuple[str, str, str]]:
        """Fetch a user's currently-valid facts (effective-at predicate).

        Superseded (``valid_to`` closed) and retracted (``invalid_at`` set)
        facts are excluded so they never leak into extraction prompts.

        Args:
            user_id: The user scope.
            org_id: Tenant scope.
            effective_at: Validity instant (caller passes ``now(UTC)``).
            limit: Maximum rows.

        Returns:
            ``(subject, predicate, object)`` rows, newest first.
        """
        result = await self._db.execute(
            text("""
                SELECT f.subject, f.predicate, f.object FROM facts f
                JOIN episodes e ON f.source_episode_id = e.id
                JOIN sessions s ON e.session_id = s.id
                WHERE s.user_id = :user_id AND s.organization_id = :org_id
                  AND (f.invalid_at IS NULL OR f.invalid_at > :effective_at)
                  AND (f.valid_from IS NULL OR f.valid_from <= :effective_at)
                  AND (f.valid_to IS NULL OR f.valid_to > :effective_at)
                ORDER BY f.created_at DESC
                LIMIT :limit
            """),
            {"user_id": user_id, "org_id": org_id, "effective_at": effective_at},
        )
        return [(row[0], row[1], row[2]) for row in result.fetchall()]

    async def list_user_episode_ids(
        self,
        user_id: UUID,
        org_id: UUID,
        project_id: UUID | None,
        cap: int,
    ) -> list[tuple[UUID, UUID]]:
        """Fetch a user's recent ``(episode_id, project_id)`` pairs for entity lookup.

        Args:
            user_id: The user scope.
            org_id: Tenant scope.
            project_id: Optional project narrowing (same result, fewer rows).
            cap: Bound on the IN-list (most-recent episodes first).

        Returns:
            ``(episode_id, project_id)`` rows, newest first.
        """
        params: dict[str, Any] = {"user_id": user_id, "org_id": org_id}
        project_filter = ""
        if project_id is not None:
            project_filter = "AND e.project_id = :project_id"
            params["project_id"] = project_id
        params["cap"] = cap
        # project_filter is a static literal ("" or a fixed AND clause), never
        # user input; all values are bound parameters — not an injection vector.
        result = await self._db.execute(
            text(
                "SELECT e.id, e.project_id "  # noqa: S608
                "FROM episodes e "  # noqa: S608
                "JOIN sessions s ON e.session_id = s.id "  # noqa: S608
                "WHERE s.user_id = :user_id AND s.organization_id = :org_id "  # noqa: S608
                "AND e.is_deleted = false AND s.is_deleted = false "  # noqa: S608
                f"{project_filter} "  # noqa: S608
                "ORDER BY e.created_at DESC "  # noqa: S608
                "LIMIT :cap"  # noqa: S608
            ),
            params,
        )
        return [(row[0], row[1]) for row in result.fetchall()]

    async def list_user_classifications(
        self, user_id: UUID, org_id: UUID, limit: int
    ) -> list[tuple[str | None, str | None]]:
        """Fetch aggregate ``(intent, emotion)`` rows for a user's summary.

        Archived projects are excluded: their episodes stopped being
        classified, so including them would let stale conversations dominate
        the generated summary.

        Args:
            user_id: The user scope.
            org_id: Tenant scope.
            limit: Maximum aggregated rows.

        Returns:
            ``(intent, emotion)`` rows ordered by frequency, descending.
        """
        result = await self._db.execute(
            text("""
                SELECT intent, emotion, COUNT(*) as cnt
                FROM dialog_classifications dc
                JOIN episodes e ON dc.episode_id = e.id
                JOIN sessions s ON e.session_id = s.id
                JOIN projects p ON s.project_id = p.id
                WHERE s.user_id = :user_id AND s.organization_id = :org_id
                  AND p.is_archived = false
                GROUP BY intent, emotion
                ORDER BY cnt DESC
                LIMIT :limit
            """),
            {"user_id": user_id, "org_id": org_id, "limit": limit},
        )
        return [(row[0], row[1]) for row in result.fetchall()]
