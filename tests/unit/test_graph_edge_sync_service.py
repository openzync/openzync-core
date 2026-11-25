"""Unit tests for GraphEdgeSyncService — D1 rule + ARQ enqueue routing.

Covers the D1 supersession rule (cases 1/2/3) via the pure
``compute_expiry_commands`` function and the enqueue routing
(all backends are external — SurrealDB/FalkorDB — so expiry is
enqueued as ARQ tasks) via mocked backends.  No I/O — the ARQ pool
and backends are mocked at the service boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from services.graph_edge_sync_service import (
    EdgeKey,
    GraphEdgeSyncService,
    SupersessionEvent,
    compute_expiry_commands,
    edge_key_for_fact,
    make_supersession_event,
)

pytestmark = pytest.mark.unit

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")
SRC_ENTITY = UUID("00000000-0000-0000-0000-00000000aaaa")
TGT_ENTITY = UUID("00000000-0000-0000-0000-00000000bbbb")
OTHER_ENTITY = UUID("00000000-0000-0000-0000-00000000cccc")
OLD_FACT = UUID("00000000-0000-0000-0000-000000000100")
NEW_FACT = UUID("00000000-0000-0000-0000-000000000101")

AT_TIME = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _fact(
    *,
    fact_id: UUID = OLD_FACT,
    subject_entity_id: UUID | None = SRC_ENTITY,
    object_entity_id: UUID | None = TGT_ENTITY,
    predicate: str | None = "reports_to",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=fact_id,
        subject="Alice",
        predicate=predicate,
        object="Acme",
        subject_entity_id=subject_entity_id,
        object_entity_id=object_entity_id,
    )


class TestEdgeKeyForFact:
    """Edge key derivation — only when both entity IDs resolve."""

    def test_resolved_entities_yield_edge_key(self) -> None:
        key = edge_key_for_fact(_fact())
        assert key == EdgeKey(
            source_id=SRC_ENTITY,
            target_id=TGT_ENTITY,
            relationship_type="reports_to",
        )

    def test_literal_object_yields_none(self) -> None:
        """Literal-subject facts never create edges — no key."""
        assert edge_key_for_fact(_fact(object_entity_id=None)) is None

    def test_literal_subject_yields_none(self) -> None:
        assert edge_key_for_fact(_fact(subject_entity_id=None)) is None

    def test_missing_predicate_yields_none(self) -> None:
        assert edge_key_for_fact(_fact(predicate=None)) is None


class TestMakeSupersessionEvent:
    """Event recording — successor re-assertion flag per D1."""

    def test_same_key_successor_reasserts(self) -> None:
        event = make_supersession_event(
            _fact(), _fact(fact_id=NEW_FACT), {"predicate": "reports_to"}
        )
        assert event.old_fact_id == OLD_FACT
        assert event.new_fact_id == NEW_FACT
        assert event.successor_reasserts is True

    def test_different_key_successor_does_not_reassert(self) -> None:
        event = make_supersession_event(
            _fact(), _fact(fact_id=NEW_FACT, object_entity_id=OTHER_ENTITY), {}
        )
        assert event.successor_reasserts is False

    def test_retraction_never_reasserts(self) -> None:
        event = make_supersession_event(_fact(), None, {})
        assert event.new_fact_id is None
        assert event.successor_reasserts is False

    def test_literal_old_fact_has_no_edge_key(self) -> None:
        event = make_supersession_event(_fact(object_entity_id=None), None, {})
        assert event.old_edge_key is None


class TestComputeExpiryCommands:
    """The D1 rule: case 1 expire, case 2 expire, case 3 skip."""

    def _command_for(self, event: SupersessionEvent):
        commands = compute_expiry_commands(
            [event], user_id=ORG_ID, project_id=PROJECT_ID, at_time=AT_TIME
        )
        assert len(commands) == 1
        return commands[0]

    def test_case1_no_successor_expires(self) -> None:
        """Retraction — superseded fact has no successor → expire."""
        event = make_supersession_event(_fact(), None, {})
        command = self._command_for(event)
        assert command.source_id == SRC_ENTITY
        assert command.target_id == TGT_ENTITY
        assert command.relationship_type == "reports_to"
        assert command.at_time == AT_TIME
        assert command.fact_id == OLD_FACT

    def test_case2_successor_different_key_expires(self) -> None:
        """Entity flip-flop — successor asserts a different key → expire."""
        event = make_supersession_event(
            _fact(), _fact(fact_id=NEW_FACT, object_entity_id=OTHER_ENTITY), {}
        )
        self._command_for(event)

    def test_case3_successor_same_key_skips(self) -> None:
        """Successor re-asserts the same edge key → keep the edge."""
        event = make_supersession_event(
            _fact(), _fact(fact_id=NEW_FACT), {"predicate": "reports_to"}
        )
        commands = compute_expiry_commands(
            [event], user_id=ORG_ID, project_id=PROJECT_ID, at_time=AT_TIME
        )
        assert commands == []

    def test_literal_fact_never_expires(self) -> None:
        """Literal subject/object — no edge was created, nothing to expire."""
        event = make_supersession_event(_fact(object_entity_id=None), None, {})
        commands = compute_expiry_commands(
            [event], user_id=ORG_ID, project_id=PROJECT_ID, at_time=AT_TIME
        )
        assert commands == []

    def test_mixed_events_reduce_to_correct_expire_set(self) -> None:
        """One retraction + one flip-flop + one re-assertion → 2 commands."""
        events = [
            make_supersession_event(_fact(), None, {}),  # case 1 → expire
            make_supersession_event(  # case 2 → expire
                _fact(fact_id=UUID("00000000-0000-0000-0000-000000000200")),
                _fact(fact_id=NEW_FACT, object_entity_id=OTHER_ENTITY),
                {},
            ),
            make_supersession_event(  # case 3 → skip
                _fact(fact_id=UUID("00000000-0000-0000-0000-000000000300")),
                _fact(fact_id=UUID("00000000-0000-0000-0000-000000000301")),
                {"predicate": "reports_to"},
            ),
        ]
        commands = compute_expiry_commands(
            events, user_id=ORG_ID, project_id=PROJECT_ID, at_time=AT_TIME
        )
        assert len(commands) == 2


class TestSyncRouting:
    """Enqueue routing — external backends enqueue ARQ expiry tasks."""

    def _case1_events(self) -> list[SupersessionEvent]:
        return [make_supersession_event(_fact(), None, {})]

    async def test_external_backend_enqueues_arq_task(self) -> None:
        """Surreal/Falkor backends enqueue expire_graph_edges on the low queue."""
        external = AsyncMock()
        arq = AsyncMock()
        service = GraphEdgeSyncService(backends=[external], arq_pool=arq)
        await service.sync_supersessions(
            user_id=ORG_ID, project_id=PROJECT_ID,
            events=self._case1_events(), at_time=AT_TIME,
        )

        # The external backend itself is never called directly.
        external.expire_relationships_matching.assert_not_awaited()
        arq.enqueue.assert_awaited_once_with(
            "expire_graph_edges",
            queue_name="OpenZync:test:queue:low",
            user_id=str(ORG_ID),
            project_id=str(PROJECT_ID),
            source_id=str(SRC_ENTITY),
            target_id=str(TGT_ENTITY),
            relationship_type="reports_to",
            at_time=AT_TIME,
            fact_id=str(OLD_FACT),
        )

    async def test_no_events_is_noop(self) -> None:
        """Empty event list → no backend calls, no enqueues."""
        external = AsyncMock()
        arq = AsyncMock()

        service = GraphEdgeSyncService(
            backends=[external], arq_pool=arq
        )
        await service.sync_supersessions(
            user_id=ORG_ID, project_id=PROJECT_ID,
            events=[], at_time=AT_TIME,
        )
        external.expire_relationships_matching.assert_not_awaited()
        arq.enqueue.assert_not_awaited()

    async def test_no_backends_is_noop(self) -> None:
        """Graph disabled (no backends) → nothing happens."""
        service = GraphEdgeSyncService(backends=[])
        await service.sync_supersessions(
            user_id=ORG_ID, project_id=PROJECT_ID,
            events=self._case1_events(), at_time=AT_TIME,
        )
        # No assertion needed beyond not raising — covered by early return.
