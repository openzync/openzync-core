"""Service layer for the user summary feature.

Coordinates summary generation (trigger + background ARQ task), summary
retrieval, and custom-instruction management (CRUD for the ``user_summary``
scope).  All tenant isolation is enforced at the repository layer.
"""

from __future__ import annotations

from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from core.arq import ARQPool
from core.exceptions import RateLimitError
from repositories.custom_instruction_repository import CustomInstructionRepository
from repositories.user_repository import UserRepository
from schemas.user_summary import UserSummaryResponse, UserSummaryTriggerResponse


class UserSummaryService:
    """Business logic for user summary generation and retrieval.

    Wires together DB access (UserRepository, CustomInstructionRepository),
    the ARQ background queue, and optional Redis rate limiting.
    """

    def __init__(
        self,
        db: AsyncSession,
        arq: ARQPool,
        redis: Redis | None = None,
    ) -> None:
        """Initialise the service with its dependencies.

        Args:
            db: Async SQLAlchemy session scoped to the current request.
            arq: ARQ pool for enqueueing background ``generate_user_summary``
                jobs.
            redis: Optional Redis client for rate limiting.  When ``None``
                rate limiting is disabled (graceful degradation).
        """
        self._db = db
        self._user_repo = UserRepository(db)
        self._ci_repo = CustomInstructionRepository(db)
        self._arq = arq
        self._redis = redis

    # ── Rate limiting ──────────────────────────────────────────────────────────

    async def _check_rate_limit(self, org_id: UUID, user_id: UUID) -> bool:
        """Enforce a 5-minute cooldown between summary generations per user.

        Uses Redis ``SET NX EX`` (``SET`` if not exists, TTL = 300 s).
        Returns ``True`` if the request is allowed, ``False`` if the key
        already exists (rate limited).

        Args:
            org_id: Organization UUID (scoping key).
            user_id: User UUID (scoping key).

        Returns:
            ``True`` if the operation may proceed, ``False`` if rate limited.
        """
        if self._redis is None:
            return True  # No Redis = no rate limiting (graceful degradation)
        key = f"ratelimit:summary:{org_id}:{user_id}"
        ok = await self._redis.set(key, "1", nx=True, ex=300)
        return bool(ok)

    # ── Trigger generation ─────────────────────────────────────────────────────

    async def trigger_generation(
        self,
        org_id: UUID,
        user_id: UUID,
    ) -> UserSummaryTriggerResponse:
        """Enqueue a background user summary generation job.

        Checks the rate limit first (one generation per 5 minutes per user).
        If allowed, enqueues a ``generate_user_summary`` ARQ task and returns
        immediately with a ``processing`` status.

        Args:
            org_id: Organization UUID (tenant scope).
            user_id: User UUID to generate a summary for.

        Returns:
            A ``UserSummaryTriggerResponse`` confirming the job was enqueued.

        Raises:
            RateLimitError: If a generation was already triggered within the
                last 5 minutes for this user.
        """
        if not await self._check_rate_limit(org_id, user_id):
            raise RateLimitError(
                "Summary generation rate limited. "
                "Please wait 5 minutes before trying again.",
            )

        import uuid

        await self._arq.enqueue(
            "generate_user_summary",
            org_id=str(org_id),
            user_id=str(user_id),
            trace_id=str(uuid.uuid4()),
        )

        return UserSummaryTriggerResponse(
            message="Summary generation started.",
            user_id=user_id,
        )

    # ── Read summary ───────────────────────────────────────────────────────────

    async def get_summary(
        self,
        org_id: UUID,
        user_id: UUID,
    ) -> UserSummaryResponse | None:
        """Fetch the currently stored summary for a user.

        Args:
            org_id: Organization UUID (tenant scope).
            user_id: User UUID to fetch the summary for.

        Returns:
            A ``UserSummaryResponse`` with the summary text and timestamp,
            or ``None`` if no summary has been generated yet.
        """
        # ⚠️  `org_id` is accepted for future tenant-isolation enforcement,
        # but the underlying ``UserRepository.get_summary`` currently queries
        # by ``user_id`` only.  If users span organisations a where clause on
        # ``organization_id`` must be added to the query.
        summary, updated_at = await self._user_repo.get_summary(user_id)
        if summary is None:
            return None
        return UserSummaryResponse(
            user_id=user_id,
            summary=summary,
            updated_at=updated_at,
        )

    # ── Custom instructions CRUD (user_summary scope) ──────────────────────────

    async def get_instructions(
        self,
        org_id: UUID,
        user_id: UUID,
    ) -> list[dict[str, str]]:
        """Fetch custom instructions scoped to ``user_summary`` for this user.

        Args:
            org_id: Organization UUID (tenant scope).
            user_id: User UUID (target of the instructions).

        Returns:
            A list of ``{"name": ..., "text": ...}`` dicts, ordered
            alphabetically by name.  Empty list when no instructions exist.
        """
        raw = await self._ci_repo.get_by_scope(
            org_id=org_id,
            scope="user_summary",
            target_id=user_id,
        )
        return [{"name": i.name, "text": i.text} for i in raw]

    async def set_instructions(
        self,
        org_id: UUID,
        user_id: UUID,
        instructions: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        """Atomically replace all custom instructions for this scope + target.

        Deletes all existing ``user_summary`` instructions for
        ``(org_id, user_id)`` and bulk-inserts the new ones in a single
        transaction.

        Args:
            org_id: Organization UUID (tenant scope).
            user_id: User UUID (target of the instructions).
            instructions: List of ``{"name": ..., "text": ...}`` dicts.
                Each name must be unique within this scope + target.

        Returns:
            The newly created instruction dicts with server-side defaults
            populated.
        """
        raw = await self._ci_repo.set_by_scope(
            org_id=org_id,
            scope="user_summary",
            target_id=user_id,
            instructions=instructions,
        )
        return [{"name": i.name, "text": i.text} for i in raw]

    async def delete_instructions(
        self,
        org_id: UUID,
        user_id: UUID,
    ) -> None:
        """Delete all custom instructions for this scope + target.

        Args:
            org_id: Organization UUID (tenant scope).
            user_id: User UUID (target of the instructions).
        """
        await self._ci_repo.delete_by_scope(
            org_id=org_id,
            scope="user_summary",
            target_id=user_id,
        )
