"""Webhook event type registry and typed payload definitions.

Every meaningful action in the system maps to an event type constant and an
optional typed payload.  Services call ``WebhookService.emit()`` with an event
type and payload dict; the webhook service enqueues an ARQ job to deliver the
webhook asynchronously.

Event type strings follow the pattern ``{domain}.{action}``, e.g.:
  ``session.created``, ``episode.processed``, ``fact.extracted``

Usage::

    from core.events import EventType, event_metadata
    from services.webhook_service import WebhookService

    await webhook_service.emit(
        org_id=org.id,
        event_type=EventType.SESSION_CREATED,
        payload={"session_id": str(session.id), "user_id": str(user.id)},
    )
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar, NamedTuple


class EventType(str):
    """A webhook event type constant.

    Usage::

        event = EventType.SESSION_CREATED
        assert event == "session.created"
    """

    SESSION_CREATED: ClassVar[str] = "session.created"
    SESSION_CLOSED: ClassVar[str] = "session.closed"
    MESSAGE_ADDED: ClassVar[str] = "message.added"
    EPISODE_PROCESSED: ClassVar[str] = "episode.processed"
    INGEST_BATCH_COMPLETED: ClassVar[str] = "ingest.batch.completed"
    INGEST_EPISODE_COMPLETED: ClassVar[str] = "ingest.episode.completed"
    GRAPH_ENTITY_CREATED: ClassVar[str] = "graph.entity.created"
    GRAPH_ENTITY_UPDATED: ClassVar[str] = "graph.entity.updated"
    GRAPH_EDGE_CREATED: ClassVar[str] = "graph.edge.created"
    FACT_EXTRACTED: ClassVar[str] = "fact.extracted"
    FACT_DELETED: ClassVar[str] = "fact.deleted"
    CLASSIFICATION_CREATED: ClassVar[str] = "classification.created"
    EXTRACTION_CREATED: ClassVar[str] = "extraction.created"
    USER_CREATED: ClassVar[str] = "user.created"


# ── Event metadata ──────────────────────────────────────────────────────────────


class EventMeta(NamedTuple):
    """Metadata about a registered event type."""

    type: str
    label: str
    category: str
    description: str


# All known events — used by the create-webhook UI and the event registry.
EVENT_REGISTRY: list[EventMeta] = [
    EventMeta(EventType.SESSION_CREATED, "Session Created", "Session", "Fired when a new conversation session is created"),
    EventMeta(EventType.SESSION_CLOSED, "Session Closed", "Session", "Fired when a session is closed"),
    EventMeta(EventType.MESSAGE_ADDED, "Message Added", "Message", "Fired when a message is added to a session"),
    EventMeta(EventType.EPISODE_PROCESSED, "Episode Processed", "Graph", "Fired when an episode finishes processing into the graph"),
    EventMeta(EventType.INGEST_BATCH_COMPLETED, "Ingest Batch Completed", "Graph", "Fired when a batch ingestion operation completes"),
    EventMeta(EventType.INGEST_EPISODE_COMPLETED, "Ingest Episode Completed", "Graph", "Fired when a single-episode ingestion completes"),
    EventMeta(EventType.GRAPH_ENTITY_CREATED, "Graph Entity Created", "Graph", "Fired when a new graph entity (node) is created"),
    EventMeta(EventType.GRAPH_ENTITY_UPDATED, "Graph Entity Updated", "Graph", "Fired when a graph entity is updated"),
    EventMeta(EventType.GRAPH_EDGE_CREATED, "Graph Edge Created", "Graph", "Fired when a relationship edge is created between entities"),
    EventMeta(EventType.FACT_EXTRACTED, "Fact Extracted", "Fact", "Fired when a fact (triple) is extracted"),
    EventMeta(EventType.FACT_DELETED, "Fact Deleted", "Fact", "Fired when a fact is deleted"),
    EventMeta(EventType.CLASSIFICATION_CREATED, "Classification Created", "Classification", "Fired when a dialog classification is created"),
    EventMeta(EventType.EXTRACTION_CREATED, "Extraction Created", "Extraction", "Fired when a structured extraction is created"),
    EventMeta(EventType.USER_CREATED, "User Created", "User", "Fired when a new user is created"),
]


def event_type_labels() -> Mapping[str, str]:
    """Return a mapping of event type → human-readable label."""
    return {meta.type: meta.label for meta in EVENT_REGISTRY}


def event_categories() -> Mapping[str, list[EventMeta]]:
    """Return event registry grouped by category."""
    categories: dict[str, list[EventMeta]] = {}
    for meta in EVENT_REGISTRY:
        categories.setdefault(meta.category, []).append(meta)
    return categories
