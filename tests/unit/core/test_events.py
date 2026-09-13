"""Unit tests for webhook event type registry and metadata."""

from __future__ import annotations

import re

import pytest


@pytest.mark.unit
class TestEventTypeConstants:
    """EventType string constants match domain.action pattern."""

    def test_session_created_value(self) -> None:
        from core.events import EventType
        assert EventType.SESSION_CREATED == "session.created"
        assert isinstance(EventType.SESSION_CREATED, str)

    def test_session_closed_value(self) -> None:
        from core.events import EventType
        assert EventType.SESSION_CLOSED == "session.closed"

    def test_message_added_value(self) -> None:
        from core.events import EventType
        assert EventType.MESSAGE_ADDED == "message.added"

    def test_episode_processed_value(self) -> None:
        from core.events import EventType
        assert EventType.EPISODE_PROCESSED == "episode.processed"

    def test_ingest_batch_completed_value(self) -> None:
        from core.events import EventType
        assert EventType.INGEST_BATCH_COMPLETED == "ingest.batch.completed"

    def test_ingest_episode_completed_value(self) -> None:
        from core.events import EventType
        assert EventType.INGEST_EPISODE_COMPLETED == "ingest.episode.completed"

    def test_graph_entity_created_value(self) -> None:
        from core.events import EventType
        assert EventType.GRAPH_ENTITY_CREATED == "graph.entity.created"

    def test_graph_entity_updated_value(self) -> None:
        from core.events import EventType
        assert EventType.GRAPH_ENTITY_UPDATED == "graph.entity.updated"

    def test_graph_edge_created_value(self) -> None:
        from core.events import EventType
        assert EventType.GRAPH_EDGE_CREATED == "graph.edge.created"

    def test_fact_extracted_value(self) -> None:
        from core.events import EventType
        assert EventType.FACT_EXTRACTED == "fact.extracted"

    def test_fact_deleted_value(self) -> None:
        from core.events import EventType
        assert EventType.FACT_DELETED == "fact.deleted"

    def test_classification_created_value(self) -> None:
        from core.events import EventType
        assert EventType.CLASSIFICATION_CREATED == "classification.created"

    def test_extraction_created_value(self) -> None:
        from core.events import EventType
        assert EventType.EXTRACTION_CREATED == "extraction.created"

    def test_user_created_value(self) -> None:
        from core.events import EventType
        assert EventType.USER_CREATED == "user.created"

    def test_all_event_types_follow_domain_action_pattern(self) -> None:
        """Every event type constant follows {domain}.{action} pattern."""
        from core.events import EventType

        pattern = re.compile(r"^[a-z]+(?:\.[a-z]+)+$")
        # Collect all ClassVar string values on EventType
        for attr_name in dir(EventType):
            attr = getattr(EventType, attr_name)
            if isinstance(attr, str) and attr_name.isupper():
                assert pattern.match(attr), (
                    f"EventType.{attr_name} = '{attr}' does not match "
                    f"expected '{{domain}}.{{action}}' pattern"
                )

    def test_event_type_is_subclass_of_str(self) -> None:
        """EventType inherits from str so it can be compared directly."""
        from core.events import EventType
        assert issubclass(EventType, str)
        assert isinstance(EventType.SESSION_CREATED, str)


@pytest.mark.unit
class TestEventMeta:
    """EventMeta named tuple metadata."""

    def test_metadata_contains_all_fields(self) -> None:
        """EventMeta has type, label, category, description fields."""
        from core.events import EventMeta

        meta = EventMeta(
            type="session.created",
            label="Session Created",
            category="Session",
            description="Fired when a new session is created",
        )
        assert meta.type == "session.created"
        assert meta.label == "Session Created"
        assert meta.category == "Session"
        assert meta.description == "Fired when a new session is created"

    def test_metadata_is_immutable(self) -> None:
        """EventMeta named tuple fields cannot be reassigned."""
        from core.events import EventMeta

        meta = EventMeta("test", "Test", "Test", "Desc")
        with pytest.raises(AttributeError):
            meta.type = "changed"  # type: ignore[misc]


@pytest.mark.unit
class TestEventRegistry:
    """EVENT_REGISTRY contains all event types with metadata."""

    def test_all_event_types_are_in_registry(self) -> None:
        """Every EventType constant has an entry in EVENT_REGISTRY."""
        from core.events import EVENT_REGISTRY, EventType

        registered_types = {meta.type for meta in EVENT_REGISTRY}
        # Collect ClassVar string values
        for attr_name in dir(EventType):
            attr = getattr(EventType, attr_name)
            if isinstance(attr, str) and attr_name.isupper():
                assert attr in registered_types, (
                    f"EventType.{attr_name} = '{attr}' is missing from EVENT_REGISTRY"
                )

    def test_registry_contains_no_duplicates(self) -> None:
        """EVENT_REGISTRY has no duplicate event types."""
        from core.events import EVENT_REGISTRY

        types = [meta.type for meta in EVENT_REGISTRY]
        assert len(types) == len(set(types)), "Duplicate event types in EVENT_REGISTRY"

    def test_registry_count_matches_event_type_classvars(self) -> None:
        """Number of registry entries equals number of EventType constants."""
        from core.events import EVENT_REGISTRY, EventType

        event_count = sum(
            1 for attr_name in dir(EventType)
            if isinstance(getattr(EventType, attr_name), str) and attr_name.isupper()
        )
        assert len(EVENT_REGISTRY) == event_count

    def test_registry_entries_have_non_empty_descriptions(self) -> None:
        """Every registry entry has a non-empty description."""
        from core.events import EVENT_REGISTRY

        for meta in EVENT_REGISTRY:
            assert meta.description, f"Empty description for {meta.type}"

    def test_registry_entries_have_non_empty_labels(self) -> None:
        """Every registry entry has a non-empty label."""
        from core.events import EVENT_REGISTRY

        for meta in EVENT_REGISTRY:
            assert meta.label, f"Empty label for {meta.type}"


@pytest.mark.unit
class TestEventHelpers:
    """event_type_labels and event_categories helper functions."""

    def test_event_type_labels_returns_mapping(self) -> None:
        """event_type_labels returns type → label mapping."""
        from core.events import event_type_labels

        labels = event_type_labels()
        assert labels["session.created"] == "Session Created"
        assert labels["episode.processed"] == "Episode Processed"
        assert len(labels) >= 14  # all event types

    def test_event_categories_groups_by_category(self) -> None:
        """event_categories returns events grouped by category."""
        from core.events import event_categories

        categories = event_categories()
        assert "Session" in categories
        assert "Graph" in categories
        assert isinstance(categories["Session"], list)
        # All entries in a category have that category
        for meta in categories["Graph"]:
            assert meta.category == "Graph"

    def test_event_categories_coverage(self) -> None:
        """All events are covered across all categories."""
        from core.events import EVENT_REGISTRY, event_categories

        categories = event_categories()
        categorized_count = sum(len(items) for items in categories.values())
        assert categorized_count == len(EVENT_REGISTRY)
