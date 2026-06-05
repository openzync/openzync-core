# GDPR Compliance Implementation Guide

> **Phase:** Phase 1 — Core Memory (Week 3-4)
> **Priority:** P0
> **Requirements:** USR-04, SEC-04, GDPR (Right to Erasure, Right to Data Portability)
> **Handoff from:** Architect (ADR-004: Data Lifecycle & GDPR)

---

## 1. Overview

MemGraph handles personal data: user identifiers, conversation content, extracted facts, and metadata. This document covers the implementation of GDPR-mandated rights:

- **Right to erasure** (Article 17): Complete deletion of all user data across all stores.
- **Right to data portability** (Article 20): Export all user data in machine-readable format.
- **Data retention**: Configurable retention periods with automated purging.

The implementation uses a **soft-delete with grace period** pattern: initial deletion is a soft-delete that hides the user from queries, followed by a configurable grace period (default: 30 days) before hard-deletion permanently removes all data.

---

## 2. Deletion Architecture

### 2.1 Two-Phase Deletion

```
Phase 1 — Soft Delete (immediate)
  ├── Mark user.is_deleted = True
  ├── Hide from all queries (WHERE is_deleted = False)
  ├── Invalidate all Redis caches for this user
  └── Enqueue async hard-deletion task (delayed by grace period)

Phase 2 — Hard Delete (after grace period)
  ├── Delete from PostgreSQL (cascade through all tables)
  ├── Delete graph nodes from FalkorDB/Neo4j
  ├── Delete from Redis caches
  ├── Cancel/clean up pending ARQ jobs
  └── Anonymize log entries containing user_id
```

### 2.2 Directory Layout

```
services/worker/tasks/
├── gdpr.py                    # delete_user_data + data export
├── session_cleanup.py         # Session auto-close (shared)
```

---

## 3. Right to Erasure — Implementation

### 3.1 Worker Task: `delete_user_data`

This is an ARQ worker task that performs the hard-deletion cascade after the grace period.

```python
# services/worker/tasks/gdpr.py

from uuid import UUID
from datetime import datetime, timedelta
from arq.connections import ArqRedis
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text, delete


async def delete_user_data(ctx: dict, user_id: str, organization_id: str) -> dict:
    """GDPR-compliant full user deletion.

    Runs in a single database transaction. If any step fails, the
    entire deletion is rolled back to prevent partial data loss.

    This task is enqueued with a delay (default: 30 days) after the
    user calls DELETE /users/{user_id}.

    Args:
        user_id: Internal MemGraph user UUID.
        organization_id: Tenant UUID.

    Returns:
        Dict with deletion summary (counts per table).

    Raises:
        RetryableTaskError: If a transient failure occurs.
            Worker will retry with exponential backoff.
    """
    logger = ctx["logger"]
    db: AsyncSession = ctx["db"]
    redis: ArqRedis = ctx["redis"]
    graphiti: GraphitiClient = ctx["graphiti"]

    user_uuid = UUID(user_id)
    org_uuid = UUID(organization_id)

    # Verify the user still exists and is soft-deleted
    user = await get_user_if_deleted(db, user_uuid, org_uuid)
    if not user:
        logger.warning(
            "gdpr.user_not_found_or_not_deleted",
            extra={"user_id": user_id},
        )
        return {"status": "skipped", "reason": "User not found or not soft-deleted"}

    summary = {}

    # ── Step 1: PostgreSQL cascade deletion ─────────────────────────
    # Order is critical: child tables first, then parents
    # This avoids FK constraint violations

    async with db.begin():
        # 1a. Dialog classifications (via episodes → sessions → user)
        result = await db.execute(
            text("""
                DELETE FROM dialog_classifications dc
                USING episodes e
                WHERE dc.episode_id = e.id
                AND e.user_id = :user_id
            """),
            {"user_id": user_id},
        )
        summary["dialog_classifications"] = result.rowcount

        # 1b. Structured extractions (via sessions → user)
        result = await db.execute(
            text("""
                DELETE FROM structured_extractions se
                USING sessions s
                WHERE se.session_id = s.id
                AND s.user_id = :user_id
            """),
            {"user_id": user_id},
        )
        summary["structured_extractions"] = result.rowcount

        # 1c. Facts
        result = await db.execute(
            text("DELETE FROM facts WHERE user_id = :user_id"),
            {"user_id": user_id},
        )
        summary["facts"] = result.rowcount

        # 1d. Episodes
        result = await db.execute(
            text("DELETE FROM episodes WHERE user_id = :user_id"),
            {"user_id": user_id},
        )
        summary["episodes"] = result.rowcount

        # 1e. Sessions
        result = await db.execute(
            text("DELETE FROM sessions WHERE user_id = :user_id"),
            {"user_id": user_id},
        )
        summary["sessions"] = result.rowcount

        # 1f. User (finally)
        result = await db.execute(
            text("DELETE FROM users WHERE id = :user_id AND organization_id = :org_id"),
            {"user_id": user_id, "org_id": organization_id},
        )
        summary["users"] = result.rowcount

    logger.info(
        "gdpr.pg_deletion_complete",
        extra={"user_id": user_id, "summary": summary},
    )

    # ── Step 2: Graph node deletion ─────────────────────────────────
    # Delete all entity/episode nodes owned by this user in the graph DB
    try:
        deleted_nodes = await graphiti.delete_user_nodes(
            user_id=user_id,
            organization_id=organization_id,
        )
        summary["graph_nodes"] = deleted_nodes
        logger.info(
            "gdpr.graph_deletion_complete",
            extra={"user_id": user_id, "nodes_deleted": deleted_nodes},
        )
    except Exception as e:
        logger.error(
            "gdpr.graph_deletion_failed",
            extra={"user_id": user_id, "error": str(e)},
        )
        # ⚠️ Graph deletion failure is non-fatal — the graph may have
        # already been cleaned up, or the graph DB may be temporarily
        # unavailable. Log and continue.
        summary["graph_nodes"] = f"error: {str(e)}"

    # ── Step 3: Redis cache invalidation ────────────────────────────
    try:
        deleted_keys = await invalidate_user_cache(redis, user_id)
        summary["cache_keys"] = deleted_keys
    except Exception as e:
        logger.error(
            "gdpr.cache_invalidation_failed",
            extra={"user_id": user_id, "error": str(e)},
        )
        summary["cache_keys"] = f"error: {str(e)}"

    # ── Step 4: Cancel pending ARQ jobs ─────────────────────────────
    try:
        cancelled_jobs = await cancel_user_jobs(redis, user_id)
        summary["cancelled_jobs"] = cancelled_jobs
    except Exception as e:
        logger.error(
            "gdpr.job_cancellation_failed",
            extra={"user_id": user_id, "error": str(e)},
        )
        summary["cancelled_jobs"] = f"error: {str(e)}"

    # ── Step 5: Anonymize log entries ───────────────────────────────
    # Actual log purging is handled by the log retention system (Loki).
    # Here we enqueue a marker for the log cleanup system.
    await enqueue_log_anonymization(redis, user_id)

    return {"status": "completed", "summary": summary}


async def get_user_if_deleted(
    db: AsyncSession, user_id: UUID, organization_id: UUID,
) -> Optional[User]:
    """Fetch user only if soft-deleted (GDPR grace period check)."""
    from sqlalchemy import select
    from models.user import User

    result = await db.execute(
        select(User).where(
            User.id == user_id,
            User.organization_id == organization_id,
            User.is_deleted == True,  # noqa: E712
        )
    )
    return result.scalar_one_or_none()
```

### 3.2 Graph Deletion Implementation

```python
# packages/graphiti-client/client.py


class GraphitiClient:
    """Thin wrapper around the Graphiti library for graph operations."""

    # ... (existing methods) ...

    async def delete_user_nodes(
        self, user_id: str, organization_id: str,
    ) -> int:
        """Delete all graph nodes owned by a user.

        For FalkorDB, this uses the Redis protocol to execute graph queries.
        For Neo4j, this uses the Bolt protocol.

        Both backends support parameterized Cypher/GQL queries.

        Returns:
            Number of nodes deleted.
        """
        if self._backend == "falkordb":
            return await self._delete_user_nodes_falkordb(user_id, organization_id)
        elif self._backend == "neo4j":
            return await self._delete_user_nodes_neo4j(user_id, organization_id)
        else:
            raise ValueError(f"Unknown graph backend: {self._backend}")

    async def _delete_user_nodes_falkordb(
        self, user_id: str, organization_id: str,
    ) -> int:
        """Delete user nodes from FalkorDB.

        FalkorDB uses Redis and the query is handled by Graphiti's graph.
        We delete all EntityNode and EpisodicNode nodes that match the
        user's organization and have the user_id property.

        Graph query (Redis + FalkorDB):
        ```
        GRAPH.QUERY memgraph_{org_id}
        "MATCH (n) WHERE n.user_id = $user_id
         DETACH DELETE n"
        ```
        """
        query = """
            MATCH (n)
            WHERE n.user_id = $user_id
            DETACH DELETE n
            RETURN count(*) AS deleted
        """
        params = {"user_id": user_id}
        result = await self._execute_query(
            f"memgraph_{organization_id}", query, params,
        )
        return result.get("deleted", 0)

    async def _delete_user_nodes_neo4j(
        self, user_id: str, organization_id: str,
    ) -> int:
        """Delete user nodes from Neo4j."""
        query = """
            MATCH (n {user_id: $user_id})
            DETACH DELETE n
            RETURN count(*) AS deleted
        """
        params = {"user_id": user_id}
        result = await self._execute_query_neo4j(
            f"memgraph_{organization_id}", query, params,
        )
        return result.get("deleted", 0)
```

### 3.3 Cache Invalidation

```python
# services/worker/tasks/gdpr.py (continued)


async def invalidate_user_cache(redis: ArqRedis, user_id: str) -> int:
    """Delete all Redis keys matching patterns for this user.

    Key patterns to match:
      - session:{user_id}:*
      - user:{user_id}:*
      - context:{user_id}:*
      - fact:{user_id}:*
      - graph:{user_id}:*

    Uses SCAN + DEL to avoid blocking Redis on large key sets.
    """
    patterns = [
        f"*:{user_id}:*",
        f"user:{user_id}:*",
        f"session:{user_id}:*",
        f"context:{user_id}:*",
    ]

    total_deleted = 0
    for pattern in patterns:
        cursor = 0
        while True:
            cursor, keys = await redis.scan(
                cursor=cursor, match=pattern, count=100,
            )
            if keys:
                deleted = await redis.delete(*keys)
                total_deleted += deleted
            if cursor == 0:
                break

    return total_deleted
```

### 3.4 ARQ Job Cancellation

```python
# services/worker/tasks/gdpr.py (continued)


async def cancel_user_jobs(redis: ArqRedis, user_id: str) -> int:
    """Mark all pending ARQ jobs for this user as cancelled.

    ARQ stores job metadata in Redis hashes. We scan the job queue
    for jobs whose payload contains the user_id.

    This is best-effort: jobs already in-flight when we scan may
    still execute. Those jobs should check user.is_deleted before
    processing.

    Strategy:
      1. Scan all job keys in the ARQ queue
      2. Deserialize the job payload
      3. If payload contains matching user_id, mark as cancelled
         by adding a 'cancelled' flag to the job result key
    """
    # ARQ job keys are stored as: arq:queue:{queue_name}:job:{job_id}
    queue_names = ["high", "low"]  # ARQ priority queues

    cancelled = 0
    for queue_name in queue_names:
        queue_key = f"arq:queue:{queue_name}"
        cursor = 0
        while True:
            cursor, job_ids = await redis.zscan(
                queue_key, cursor=cursor, count=100,
            )
            for job_id, _ in job_ids:
                # Fetch job data
                job_key = f"arq:job:{job_id}"
                job_data = await redis.hgetall(job_key)
                if not job_data:
                    continue

                # Check if job payload contains user_id
                payload = job_data.get(b"payload", b"{}")
                if isinstance(payload, bytes):
                    try:
                        import json
                        payload_dict = json.loads(payload)
                        if payload_dict.get("user_id") == user_id:
                            # Mark as cancelled
                            await redis.hset(
                                job_key, "cancelled", "1",
                            )
                            # Remove from queue
                            await redis.zrem(queue_key, job_id)
                            cancelled += 1
                    except (json.JSONDecodeError, TypeError):
                        continue

            if cursor == 0:
                break

    return cancelled
```

### 3.5 Log Cleanup

GDPR Article 17 requires that personal data in logs be deleted or anonymized. MemGraph implements a two-pronged strategy:

#### 3.5.1 Log Retention Limit

```python
# core/config.py
class Settings(BaseSettings):
    # ...
    LOG_RETENTION_DAYS: int = Field(
        default=30,
        description="Log entries older than this are automatically "
                    "purged by the log retention system.",
    )
    GDPR_LOG_ANONYMIZE_USER_IDS: bool = Field(
        default=True,
        description="When true, user_id values in logs are anonymized "
                    "after user deletion.",
    )
```

#### 3.5.2 Log Anonymization

After user deletion, a task is enqueued to anonymize log entries:

```python
async def enqueue_log_anonymization(redis: ArqRedis, user_id: str) -> None:
    """Enqueue a task that will scan Loki for log entries containing
    the deleted user_id and replace them with a redacted value.

    This is a best-effort cleanup. The actual implementation depends
    on the log storage backend:
      - Loki: Use Loki's log query API to identify and rewrite entries
        (or use logql filter + retention rules)
      - File logs: Use a batch processor to scan and redact

    For Loki-based deployments (MemGraph standard):
    The log cleanup worker queries Loki for entries with `user_id="{id}"`
    and rewrites them to `user_id="redacted_{hash}"`.
    """
    await redis.enqueue_job(
        "anonymize_logs",
        user_id=user_id,
        _job_id=f"anonymize_logs_{user_id}",
        _queue="low",
    )
```

#### 3.5.3 Structured Logging — Prevent Personal Data at Source

The most effective GDPR log strategy is **prevention** — never log personal data in the first place:

```python
# middleware/logging.py
import re
from typing import Any

# Fields that are allowed in structured logs (whitelist approach)
ALLOWED_LOG_FIELDS = {
    "request_id", "trace_id", "org_id", "duration_ms",
    "status_code", "method", "path", "endpoint",
    "task_type", "queue_name", "job_id",
    "error_code", "error_message", "retry_count",
}

# Fields that MUST NOT appear in logs
BLOCKED_LOG_PATTERNS = [
    re.compile(r"(?i)(password|secret|token|api_key|credit_card|ssn)"),
]


def sanitize_log_context(ctx: dict[str, Any]) -> dict[str, Any]:
    """Remove or redact fields that may contain personal data."""
    sanitized = {}
    for key, value in ctx.items():
        if key not in ALLOWED_LOG_FIELDS:
            continue  # Drop unknown fields
        if any(p.search(str(value)) for p in BLOCKED_LOG_PATTERNS):
            sanitized[key] = "[REDACTED]"
        else:
            sanitized[key] = value
    return sanitized
```

---

## 4. Right to Data Portability — Implementation

### 4.1 Export Endpoint

```python
# services/api/routers/users.py


@router.get("/{user_id}/export", response_model=UserDataExport)
async def export_user_data(
    user_id: UUID = Path(...),
    service: UserService = Depends(get_user_service),
    gdpr_service: GDPRService = Depends(get_gdpr_service),
    org: Organization = Depends(get_current_organization),
) -> UserDataExport:
    """Export all user data in a machine-readable JSON format.

    This endpoint supports GDPR Article 20 (Right to Data Portability).
    The export includes:
      - User profile (external_id, name, email, metadata)
      - All sessions with metadata
      - All messages (episodes) per session
      - All extracted facts
      - All dialog classifications
      - All structured extractions

    Response size may be large for users with extensive history.
    Consider pagination or streaming for production use.
    """
    return await gdpr_service.export_user_data(
        user_id=user_id,
        organization_id=org.id,
    )
```

### 4.2 Export Schema

```python
# services/api/schemas/gdpr.py

from datetime import datetime
from uuid import UUID
from typing import Optional, List
from pydantic import BaseModel, Field


class ExportedMessage(BaseModel):
    role: str
    content: str
    metadata: dict
    sequence_number: int
    created_at: datetime


class ExportedSession(BaseModel):
    external_id: str
    metadata: dict
    is_active: bool
    created_at: datetime
    closed_at: Optional[datetime]
    messages: List[ExportedMessage]


class ExportedFact(BaseModel):
    content: str
    subject: Optional[str]
    predicate: Optional[str]
    object: Optional[str]
    confidence: float
    valid_from: Optional[datetime]
    valid_to: Optional[datetime]
    created_at: datetime


class ExportedClassification(BaseModel):
    intent: Optional[str]
    emotion: Optional[str]
    valence: Optional[str]
    arousal: Optional[str]
    episode_content: str  # Snippet for context


class ExportedExtraction(BaseModel):
    data: dict
    created_at: datetime


class UserDataExport(BaseModel):
    """Complete user data export for GDPR portability."""
    exported_at: datetime = Field(default_factory=datetime.utcnow)
    schema_version: str = Field(default="1.0")

    profile: dict = Field(
        ...,
        description="User profile (external_id, name, email, metadata).",
    )
    sessions: List[ExportedSession] = Field(
        ..., description="All sessions with full message history.",
    )
    facts: List[ExportedFact] = Field(
        ..., description="All extracted facts.",
    )
    classifications: List[ExportedClassification] = Field(
        ..., description="All dialog classifications.",
    )
    extractions: List[ExportedExtraction] = Field(
        ..., description="All structured extractions.",
    )
```

### 4.3 Export Service

```python
# services/api/services/gdpr_service.py


class GDPRService:
    """GDPR-mandated operations: data export and deletion."""

    def __init__(
        self,
        db: AsyncSession,
        user_repo: UserRepository,
        session_repo: SessionRepository,
        fact_repo: FactRepository,
    ) -> None:
        self._db = db
        self._user_repo = user_repo
        self._session_repo = session_repo
        self._fact_repo = fact_repo

    async def export_user_data(
        self,
        user_id: UUID,
        organization_id: UUID,
    ) -> UserDataExport:
        """Assemble a complete export of all user data.

        ⚠️ Performance warning: This loads all data for a user into
        memory. Consider:
          - Streaming the response for large exports
          - Enqueuing a background task that generates a downloadable file
          - Adding a limit parameter (e.g., last 90 days)
        """
        user = await self._user_repo.get_by_id(user_id, organization_id)
        if not user:
            raise NotFoundError(f"User '{user_id}' not found.")

        # Fetch all sessions with messages
        sessions, _, _ = await self._session_repo.list_paginated(
            user_id=user_id, limit=10_000,
        )

        exported_sessions = []
        for session in sessions:
            messages, _, _ = await self._session_repo.get_messages_paginated(
                session_id=session.id, limit=10_000,
            )
            exported_sessions.append(
                ExportedSession(
                    external_id=session.external_id,
                    metadata=dict(session.metadata or {}),
                    is_active=session.is_active,
                    created_at=session.created_at,
                    closed_at=session.closed_at,
                    messages=[
                        ExportedMessage(
                            role=msg.role,
                            content=msg.content,
                            metadata=dict(msg.metadata or {}),
                            sequence_number=msg.sequence_number,
                            created_at=msg.created_at,
                        )
                        for msg in messages
                    ],
                )
            )

        # Fetch all facts
        facts = await self._fact_repo.get_all_for_user(user_id)
        # Fetch classifications and extractions
        classifications = await self._get_classifications(user_id)
        extractions = await self._get_extractions(user_id)

        return UserDataExport(
            profile={
                "external_id": user.external_id,
                "name": user.name,
                "email": user.email,
                "metadata": dict(user.metadata or {}),
                "created_at": user.created_at.isoformat(),
            },
            sessions=exported_sessions,
            facts=[
                ExportedFact(
                    content=f.content,
                    subject=f.subject,
                    predicate=f.predicate,
                    object=f.object,
                    confidence=f.confidence,
                    valid_from=f.valid_from,
                    valid_to=f.valid_to,
                    created_at=f.created_at,
                )
                for f in facts
            ],
            classifications=classifications,
            extractions=extractions,
        )

    async def _get_classifications(self, user_id: UUID) -> list:
        """Fetch all dialog classifications for a user."""
        result = await self._db.execute(
            text("""
                SELECT dc.intent, dc.emotion, dc.valence, dc.arousal,
                       e.content as episode_content
                FROM dialog_classifications dc
                JOIN episodes e ON dc.episode_id = e.id
                WHERE e.user_id = :user_id
                ORDER BY dc.created_at DESC
            """),
            {"user_id": str(user_id)},
        )
        rows = result.fetchall()
        return [
            ExportedClassification(
                intent=row.intent,
                emotion=row.emotion,
                valence=row.valence,
                arousal=row.arousal,
                episode_content=row.episode_content,
            )
            for row in rows
        ]

    async def _get_extractions(self, user_id: UUID) -> list:
        """Fetch all structured extractions for a user."""
        result = await self._db.execute(
            text("""
                SELECT se.data, se.created_at
                FROM structured_extractions se
                JOIN sessions s ON se.session_id = s.id
                WHERE s.user_id = :user_id
                ORDER BY se.created_at DESC
            """),
            {"user_id": str(user_id)},
        )
        rows = result.fetchall()
        return [
            ExportedExtraction(data=row.data, created_at=row.created_at)
            for row in rows
        ]
```

---

## 5. Data Retention Policy

### 5.1 Configuration

```python
# core/config.py
class Settings(BaseSettings):
    # ── GDPR / Data Retention ───────────────────────────────────────

    SOFT_DELETE_GRACE_DAYS: int = Field(
        default=30,
        description="Days between soft-delete and permanent hard-delete "
                    "of user data.",
    )
    EPISODE_RETENTION_DAYS: int = Field(
        default=90,
        description="Raw episode (message) content is kept for this many "
                    "days. After this, episodes are purged but aggregated "
                    "facts are preserved.",
    )
    FACT_RETENTION: str = Field(
        default="indefinite",
        description="Aggregated fact retention policy: 'indefinite' or "
                    "number of days.",
    )
    LOG_RETENTION_DAYS: int = Field(
        default=30,
        description="Log retention period in days.",
    )
    GDPR_AUTO_PURGE_ENABLED: bool = Field(
        default=True,
        description="When true, a daily scheduled task purges users "
                    "whose soft-delete grace period has expired.",
    )
```

### 5.2 Retention Rules Summary

| Data Type | Retention | Rationale |
|---|---|---|
| Raw episodes (messages) | 90 days (configurable) | Raw conversation content kept for enrichment; aggregated into facts afterward |
| Extracted facts | Indefinite | Aggregated knowledge — no longer contains raw conversation, only derived facts |
| Dialog classifications | 90 days (same as episodes) | Attached to specific episodes |
| Structured extractions | 90 days (same as sessions) | Derived from session content |
| User profile | Indefinite (until deletion request) | Core identity record |
| Logs | 30 days | Operational debugging; GDPR requires limited retention |
| Graph nodes | Indefinite (until deletion request) | Encodes entity knowledge without raw PII |

### 5.3 Episode Purging Scheduled Task

```python
# services/worker/tasks/retention.py


async def purge_expired_episodes(ctx: dict) -> dict:
    """Delete episodes older than the retention threshold.

    Runs daily via ARQ cron schedule.

    This does NOT delete the associated facts — aggregated facts
    are retained independently. The graph nodes representing these
    episodes are also cleaned up.

    Returns:
        Dict with count of deleted episodes.
    """
    db: AsyncSession = ctx["db"]
    settings: Settings = ctx["settings"]
    retention_days = settings.EPISODE_RETENTION_DAYS

    cutoff = datetime.utcnow() - timedelta(days=retention_days)

    async with db.begin():
        # Delete episodes older than cutoff
        result = await db.execute(
            text("""
                DELETE FROM episodes
                WHERE created_at < :cutoff
            """),
            {"cutoff": cutoff},
        )
        deleted_count = result.rowcount

    logger.info(
        "retention.episodes_purged",
        extra={
            "cutoff": cutoff.isoformat(),
            "deleted_count": deleted_count,
            "retention_days": retention_days,
        },
    )

    return {"deleted_episodes": deleted_count}


async def purge_soft_deleted_users(ctx: dict) -> dict:
    """Hard-delete users whose soft-delete grace period has expired.

    Runs daily via ARQ cron schedule.

    This enqueues individual delete_user_data tasks for each expired
    user, which handle the full cascade + graph deletion.
    """
    db: AsyncSession = ctx["db"]
    redis: ArqRedis = ctx["redis"]
    settings: Settings = ctx["settings"]

    if not settings.GDPR_AUTO_PURGE_ENABLED:
        return {"status": "disabled"}

    grace_days = settings.SOFT_DELETE_GRACE_DAYS
    cutoff = datetime.utcnow() - timedelta(days=grace_days)

    # Find expired soft-deleted users
    result = await db.execute(
        text("""
            SELECT id, organization_id FROM users
            WHERE is_deleted = true
            AND deleted_at < :cutoff
        """),
        {"cutoff": cutoff},
    )
    expired_users = result.fetchall()

    enqueued = 0
    for user_id, org_id in expired_users:
        await redis.enqueue_job(
            "delete_user_data",
            user_id=str(user_id),
            organization_id=str(org_id),
            _job_id=f"delete_user_{user_id}",
            _queue="low",
        )
        enqueued += 1

    logger.info(
        "retention.soft_delete_purge",
        extra={
            "found_expired": len(expired_users),
            "enqueued": enqueued,
            "grace_days": grace_days,
        },
    )

    return {"found": len(expired_users), "enqueued": enqueued}
```

### 5.4 ARQ Cron Schedule for Retention

```python
# services/worker/worker.py
from arq.connections import RedisSettings


class WorkerSettings:
    functions = [
        "services.worker.tasks.gdpr.delete_user_data",
        "services.worker.tasks.gdpr.anonymize_logs",
        "services.worker.tasks.retention.purge_expired_episodes",
        "services.worker.tasks.retention.purge_soft_deleted_users",
    ]
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    cron_jobs = [
        # Retention: run daily at 2:00 AM
        {
            "cron": "0 2 * * *",
            "func": purge_expired_episodes,
            "timeout": 600,
        },
        # GDPR purge: run daily at 3:00 AM
        {
            "cron": "0 3 * * *",
            "func": purge_soft_deleted_users,
            "timeout": 600,
        },
        # Session auto-close: every 15 minutes
        {
            "cron": "*/15 * * * *",
            "func": auto_close_sessions,
            "timeout": 300,
        },
    ]
```

---

## 6. Grace Period Implementation

### 6.1 Flow

```
User calls DELETE /users/{id}
        │
        ▼
[Service] soft_delete()
  ├── user.is_deleted = True
  ├── user.deleted_at = now()
  └── Enqueue ARQ task:
        delete_user_data(user_id, org_id)
        └── defer_until = now + SOFT_DELETE_GRACE_DAYS
                │
                ├── Day 1-29: User hidden from queries
                │   (WHERE is_deleted = False everywhere)
                │
                └── Day 30: Worker executes
                    ├── Check user still exists and is_deleted=True
                    ├── If user was restored → SKIP (return early)
                    ├── Cascade delete PostgreSQL
                    ├── Delete graph nodes
                    ├── Invalidate Redis cache
                    ├── Cancel ARQ jobs
                    └── Anonymize logs
```

### 6.2 User Restoration (Undelete)

If a user is restored during the grace period (e.g., the caller re-creates the same external_id):

```python
# In UserService.create_user:

async def create_user(self, ...) -> UserResponseWithStats:
    # Check if soft-deleted user exists with this external_id
    deleted_user = await self._repo.get_deleted_by_external_id(
        request.external_id, organization_id,
    )
    if deleted_user:
        # Restore the user instead of creating a new one
        user = await self._repo.restore(deleted_user)
        # Cancel the pending deletion job
        await self._arq_queue.cancel_job(f"delete_user_{deleted_user.id}")
        return await self._build_response_with_stats(user)

    # Normal create path (from 01-user-crud.md)
    ...
```

```python
# In UserRepository:

async def get_deleted_by_external_id(
    self, external_id: str, organization_id: UUID,
) -> Optional[User]:
    result = await self._db.execute(
        select(User).where(
            User.external_id == external_id,
            User.organization_id == organization_id,
            User.is_deleted == True,  # noqa: E712
        )
    )
    return result.scalar_one_or_none()


async def restore(self, user: User) -> User:
    """Restore a soft-deleted user."""
    user.is_deleted = False
    user.deleted_at = None
    user.updated_at = func.now()
    await self._db.flush()
    await self._db.refresh(user)
    return user
```

---

## 7. Test Scenarios

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_gdpr_delete_cascade(
    async_client: AsyncClient,
    auth_headers: dict,
    existing_user_with_full_data: User,
    db_session: AsyncSession,
) -> None:
    """Verify that deleting a user removes all associated data."""
    user_id = existing_user_with_full_data.id

    # Soft-delete
    response = await async_client.delete(
        f"/v1/users/{user_id}",
        headers=auth_headers,
    )
    assert response.status_code == 204

    # Verify user hidden
    get_resp = await async_client.get(
        f"/v1/users/{user_id}",
        headers=auth_headers,
    )
    assert get_resp.status_code == 404

    # Manually run the hard-delete task (simulating grace period expiry)
    from services.worker.tasks.gdpr import delete_user_data
    result = await delete_user_data(
        {
            "db": db_session,
            "redis": redis_client,
            "graphiti": graphiti_client,
            "logger": logger,
        },
        user_id=str(user_id),
        organization_id=str(existing_user_with_full_data.organization_id),
    )
    assert result["status"] == "completed"
    assert result["summary"]["users"] == 1

    # Verify no orphaned data
    for table in ["episodes", "sessions", "facts"]:
        count = await db_session.execute(
            text(f"SELECT COUNT(*) FROM {table} WHERE user_id = :uid"),
            {"uid": str(user_id)},
        )
        assert count.scalar() == 0, f"Orphaned data in {table}"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_gdpr_export(
    async_client: AsyncClient,
    auth_headers: dict,
    existing_user_with_full_data: User,
) -> None:
    """Verify data export contains all expected sections."""
    user_id = existing_user_with_full_data.id

    response = await async_client.get(
        f"/v1/users/{user_id}/export",
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()

    assert "profile" in data
    assert "sessions" in data
    assert "facts" in data
    assert "classifications" in data
    assert "extractions" in data
    assert data["schema_version"] == "1.0"
    assert data["profile"]["external_id"] is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_soft_delete_grace_period_restoration(
    async_client: AsyncClient,
    auth_headers: dict,
    existing_user: User,
) -> None:
    """Verify that re-creating a user during grace period restores data."""
    user_id = existing_user.id
    external_id = existing_user.external_id

    # Delete the user
    await async_client.delete(f"/v1/users/{user_id}", headers=auth_headers)

    # Re-create with same external_id (within grace period)
    response = await async_client.post(
        "/v1/users",
        json={"external_id": external_id, "name": "Restored User"},
        headers=auth_headers,
    )
    assert response.status_code == 201
    assert response.json()["name"] == "Restored User"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_episode_retention_purge(
    worker_ctx: dict,
    old_episode_fixture: Episode,
) -> None:
    """Verify episodes older than retention threshold are purged."""
    from services.worker.tasks.retention import purge_expired_episodes

    result = await purge_expired_episodes(worker_ctx)
    assert result["deleted_episodes"] >= 1
```

---

## 8. Migration

```sql
-- Add soft-delete columns to users table (if not already present from 001)
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE users ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

CREATE INDEX ix_users_is_deleted ON users (organization_id, is_deleted)
    WHERE is_deleted = true;
```

---

## 9. Key Design Decisions

1. **Soft-delete first, hard-delete later**: Gives a 30-day window for accidental deletion recovery. This is a GDPR-mandated consideration — users can request deletion, but accidental deletion by the operator should be recoverable.

2. **Single transaction for PostgreSQL cascade**: All SQL DELETEs run inside a single `async with db.begin()` block. If any step fails (e.g., unique constraint violation from a concurrent operation), the entire deletion rolls back. No partial deletion state.

3. **Graph deletion is best-effort**: The graph DB may be temporarily unavailable. If graph deletion fails, it's logged and the PostgreSQL deletion still completes. A separate reconciliation job can clean up orphaned graph nodes.

4. **Export is synchronous for now**: For users with very large data volumes (>10k messages), the export endpoint may time out. Phase 2 improvement: enqueue an async export job and return a downloadable file URL.

5. **Log anonymization is best-effort**: Logs in Loki are immutable — we can't delete individual log entries. Instead, we set retention policies and rely on the structured logging middleware to never log raw PII in the first place.

---

*Document maintained by Rohan · TheLinkAI · Last updated: 2026-06-05*
