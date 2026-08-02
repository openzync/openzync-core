"""Graph edge synchronisation — mirrors fact supersession into graph backends.

Phase 2 closes the superseded fact's range (``valid_to = now``); Phase 3
synchronises that transition into the graph backends so edges stay
temporally correct.  Edges are triple-keyed ``(source_id, target_id,
relationship_type)`` on ``graph_relationships`` with a partial unique
index ``WHERE invalid_at IS NULL`` — at most ONE active edge per triple,
merged across facts.

The expire set is computed per the D1 supersession rule (architect ADR):

* Case 1 — a superseded fact has **no successor** re-asserting it
  (retraction): expire the edge.
* Case 2 — the successor asserts a **different edge key** (entity
  flip-flop): expire the old edge.
* Case 3 — the successor asserts the **same edge key**: DO NOT expire —
  the successor re-asserts the edge.

Literal-subject facts never resolve to entities and therefore never
create edges — nothing to expire.

Execution routing:

* Postgres backend — ``expire_relationships_matching`` is called
  directly on the backend's session.  The caller owns the commit point:
  when invoked from the post-commit effect (``FactInvalidationService``)
  the effect wraps the call in a fresh committed session so the expiry
  is durable; when invoked inside an open transaction the expiry joins
  it atomically.  Failures propagate loudly — never swallowed.
* SurrealDB / FalkorDB backends — an ARQ task ``expire_graph_edges`` is
  enqueued on the low-priority queue; the task resolves the org's
  backend inside the worker and expires there.  Idempotent via
  ``WHERE invalid_at IS NULL`` (replay → count 0).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from core.config import get_settings
from core.exceptions import ExternalServiceError
from packages.graph_backend.postgres import PostgresGraphBackend
from services.worker.worker_settings import get_queue_name

if TYPE_CHECKING:
    from core.arq import ARQPool
    from packages.graph_backend.interface import GraphBackend

logger = logging.getLogger(__name__)

# ARQ task name for external (non-Postgres) backend expiry — registered on
# the low-priority worker (``services/worker/worker.py`` LOW_QUEUE_TASKS).
EXPIRE_GRAPH_EDGES_TASK: str = "expire_graph_edges"
ARQ_QUEUE: str = "low"

# ── Domain records ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EdgeKey:
    """Triple key of a graph edge derived from a fact.

    ``source_id`` is the fact's subject entity, ``target_id`` the fact's
    object entity, ``relationship_type`` the predicate.  A fact only
    yields an edge key when BOTH entity IDs resolve — literal-subject
    facts never create edges.
    """

    source_id: UUID
    target_id: UUID
    relationship_type: str


@dataclass(frozen=True)
class SupersessionEvent:
    """One old-fact → successor transition recorded during supersession.

    Attributes:
        old_fact_id: The superseded fact's ID.
        new_fact_id: The successor fact's ID, or ``None`` for a
            retraction (case 1 — no successor re-asserts the fact).
        triple: The SPO strings (payload provenance for webhooks/logs).
        old_edge_key: Edge key of the superseded fact, or ``None`` when
            either entity ID is unresolved (literal → no edge exists).
        successor_reasserts: ``True`` when the successor asserts the
            SAME edge key (case 3 — the edge survives, nothing to expire).
    """

    old_fact_id: UUID
    new_fact_id: UUID | None
    triple: dict[str, str] = field(default_factory=dict)
    old_edge_key: EdgeKey | None = None
    successor_reasserts: bool = False


@dataclass(frozen=True)
class EdgeExpiryCommand:
    """One edge-expiry unit: the triple key + the deterministic instant."""

    org_id: UUID
    project_id: UUID
    source_id: UUID
    target_id: UUID
    relationship_type: str
    at_time: datetime
    fact_id: UUID


# ── Helpers ───────────────────────────────────────────────────────────────────


def edge_key_for_fact(fact: object) -> EdgeKey | None:
    """Derive the edge key of a fact row, or ``None`` when unresolved.

    Reads ``subject_entity_id`` / ``predicate`` / ``object_entity_id``
    attributes — compatible with the ORM ``Fact`` model and the
    ``SimpleNamespace`` stand-ins used in unit tests.

    Args:
        fact: Any object exposing the fact triple attributes.

    Returns:
        The edge key when both entity IDs resolve, else ``None``.
    """
    subject_entity_id = getattr(fact, "subject_entity_id", None)
    object_entity_id = getattr(fact, "object_entity_id", None)
    predicate = getattr(fact, "predicate", None)
    if (
        subject_entity_id is None
        or object_entity_id is None
        or predicate is None
        or not str(predicate).strip()
    ):
        return None
    return EdgeKey(
        source_id=UUID(str(subject_entity_id)),
        target_id=UUID(str(object_entity_id)),
        relationship_type=str(predicate),
    )


def make_supersession_event(
    old_fact: object, new_fact: object | None, triple: dict[str, str]
) -> SupersessionEvent:
    """Build the D1-rule event for one old-fact → successor transition.

    Args:
        old_fact: The superseded fact (must carry entity ID attributes).
        new_fact: The successor fact, or ``None`` for a retraction.
        triple: The SPO strings.

    Returns:
        A :class:`SupersessionEvent` with the edge key and the
        same-key re-assertion flag computed.
    """
    old_edge_key = edge_key_for_fact(old_fact)
    new_edge_key = edge_key_for_fact(new_fact) if new_fact is not None else None
    return SupersessionEvent(
        old_fact_id=UUID(str(getattr(old_fact, "id"))),
        new_fact_id=(
            UUID(str(getattr(new_fact, "id"))) if new_fact is not None else None
        ),
        triple=triple,
        old_edge_key=old_edge_key,
        successor_reasserts=(
            old_edge_key is not None and new_edge_key == old_edge_key
        ),
    )


def compute_expiry_commands(
    events: list[SupersessionEvent],
    *,
    org_id: UUID,
    project_id: UUID,
    at_time: datetime,
) -> list[EdgeExpiryCommand]:
    """Apply the D1 rule: reduce supersession events to an expire set.

    Args:
        events: The supersession transitions recorded during ingest.
        org_id: Tenant scope.
        project_id: Project scope.
        at_time: The supersession instant (deterministic, never a fresh
            clock read).

    Returns:
        One :class:`EdgeExpiryCommand` per edge that must be expired.
    """
    commands: list[EdgeExpiryCommand] = []
    for event in events:
        if event.old_edge_key is None:
            # Literal subject/object — no edge was ever created.
            continue
        # Case 3 (successor re-asserts the same key) → keep the edge.
        if event.new_fact_id is not None and event.successor_reasserts:
            continue
        # Case 1 (no successor) or case 2 (successor, different key).
        commands.append(
            EdgeExpiryCommand(
                org_id=org_id,
                project_id=project_id,
                source_id=event.old_edge_key.source_id,
                target_id=event.old_edge_key.target_id,
                relationship_type=event.old_edge_key.relationship_type,
                at_time=at_time,
                fact_id=event.old_fact_id,
            )
        )
    return commands


# ── Service ───────────────────────────────────────────────────────────────────


class GraphEdgeSyncService:
    """Synchronises superseded-fact transitions into graph backends.

    Args:
        backends: The resolved per-org graph backends.  Postgres backends
            are expired in-transaction on their own session; external
            backends (SurrealDB/FalkorDB) receive an ARQ task.  May be
            empty (graph disabled) — the service is a no-op.
        arq_pool: ARQ pool for external-backend task enqueueing.  When
            ``None`` the module singleton from ``core.arq.get_arq()`` is
            used (the established FastAPI/worker pattern).
    """

    def __init__(
        self,
        backends: list[GraphBackend],
        arq_pool: ARQPool | None = None,
    ) -> None:
        self._backends: list[GraphBackend] = list(backends)
        self._arq_pool = arq_pool

    @property
    def backends(self) -> list[GraphBackend]:
        """The resolved backends this service expires against (read-only)."""
        return list(self._backends)

    @property
    def arq_pool(self) -> ARQPool | None:
        """The ARQ pool used for external-backend enqueueing."""
        return self._arq_pool

    async def sync_supersessions(
        self,
        *,
        org_id: UUID,
        project_id: UUID,
        events: list[SupersessionEvent],
        at_time: datetime,
    ) -> None:
        """Compute the D1 expire set and execute it against all backends.

        Postgres backends are expired directly on their session — the
        caller owns the commit point (in-transaction when invoked before
        commit, explicit transaction when invoked from the post-commit
        effect).  A raise here is deliberate: the caller must see it.

        External backends receive one ``expire_graph_edges`` ARQ job per
        command on the low-priority queue; enqueue failures raise (the
        post-commit effect logs them loudly, the fact commit is already
        durable and unaffected).

        Args:
            org_id: Tenant scope.
            project_id: Project scope.
            events: Supersession transitions recorded during ingest.
            at_time: The supersession instant (deterministic).

        Raises:
            ExternalServiceError: If a Postgres expiry fails or an ARQ
                enqueue fails.  Never swallowed.
        """
        if not events or not self._backends:
            return
        commands = compute_expiry_commands(
            events, org_id=org_id, project_id=project_id, at_time=at_time
        )
        if not commands:
            return

        for backend in self._backends:
            if isinstance(backend, PostgresGraphBackend):
                for command in commands:
                    await backend.expire_relationships_matching(
                        org_id=command.org_id,
                        project_id=command.project_id,
                        source_id=command.source_id,
                        target_id=command.target_id,
                        relationship_type=command.relationship_type,
                        at_time=command.at_time,
                    )
            else:
                await self._enqueue_expiry(commands)

    async def _enqueue_expiry(self, commands: list[EdgeExpiryCommand]) -> None:
        """Enqueue one ``expire_graph_edges`` ARQ job per command (low queue).

        Args:
            commands: The edge-expiry commands for external backends.

        Raises:
            ExternalServiceError: If the ARQ pool is unavailable or an
                enqueue fails.  Facts are already committed — the failure
                is surfaced loudly for operator attention; the
                ``reconcile_graph_edges`` cron self-heals afterwards.
        """
        from core.arq import get_arq

        arq_pool = self._arq_pool if self._arq_pool is not None else get_arq()
        qname = get_queue_name(get_settings().ENVIRONMENT, ARQ_QUEUE)
        try:
            for command in commands:
                await arq_pool.enqueue(
                    EXPIRE_GRAPH_EDGES_TASK,
                    queue_name=qname,
                    org_id=str(command.org_id),
                    project_id=str(command.project_id),
                    source_id=str(command.source_id),
                    target_id=str(command.target_id),
                    relationship_type=command.relationship_type,
                    at_time=command.at_time,
                    fact_id=str(command.fact_id),
                )
        except Exception as exc:
            logger.error(
                "graph_edge_sync.enqueue_failed",
                extra={
                    "org_id": str(commands[0].org_id),
                    "project_id": str(commands[0].project_id),
                    "task": EXPIRE_GRAPH_EDGES_TASK,
                    "command_count": len(commands),
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=(
                    f"Failed to enqueue {EXPIRE_GRAPH_EDGES_TASK} for "
                    f"{len(commands)} edge(s): {exc}"
                ),
                detail={"org_id": str(commands[0].org_id)},
            ) from exc
