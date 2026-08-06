"""Unit tests for FactInvalidationService — supersession state machine.

Mocks the repository at the service boundary (no I/O).  Covers the
conflict-identity normalization, idempotent identical-content skip,
in-batch dedup, org-scoped conflict scan, insert-failure propagation,
and the FACT_SUPERSEDED webhook side effect.

The DB-level guarantees (FOR UPDATE serialization, transaction rollback
after truncate, adjacent-range exclusion-constraint safety) live in
``tests/integration/test_fact_supersession.py`` against real PostgreSQL.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from core.events import EventType
from services.fact_invalidation_service import (
    FactIngestionResult,
    FactInvalidationService,
    normalize_identity_term,
)
from services.graph_edge_sync_service import GraphEdgeSyncService

pytestmark = pytest.mark.unit

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")
USER_ID = UUID("00000000-0000-0000-0000-000000000003")
FACT_1_ID = UUID("00000000-0000-0000-0000-000000000100")
FACT_2_ID = UUID("00000000-0000-0000-0000-000000000101")

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _fact(**overrides) -> SimpleNamespace:
    """Build a minimal fact stand-in with the attrs the service reads."""
    return SimpleNamespace(
        id=overrides.get("id", FACT_1_ID),
        subject=overrides.get("subject", "Alice"),
        predicate=overrides.get("predicate", "likes"),
        object=overrides.get("object", "hiking"),
        content=overrides.get("content", "Alice likes hiking"),
        subject_entity_id=overrides.get("subject_entity_id"),
        object_entity_id=overrides.get("object_entity_id"),
    )


def _triple(
    subject: str = "Alice",
    predicate: str = "likes",
    obj: str = "hiking",
    content: str | None = None,
    **extra,
) -> dict:
    row = {
        "subject": subject,
        "predicate": predicate,
        "object": obj,
        "content": content or f"{subject} {predicate} {obj}",
    }
    row.update(extra)
    return row


@pytest.fixture
def mock_db() -> AsyncMock:
    db = AsyncMock(spec=AsyncSession)
    # The service registers after_commit/after_rollback hooks on the real
    # underlying sync session — a real (unbound) Session supports
    # instance-level event listening, which a Mock does not.
    db.sync_session = Session()
    return db


@pytest.fixture
def mock_repo() -> AsyncMock:
    repo = AsyncMock()
    repo.find_conflicting_active_for_update.return_value = []
    return repo


@pytest.fixture
def service(mock_db: AsyncMock, mock_repo: AsyncMock) -> FactInvalidationService:
    return FactInvalidationService(db=mock_db, fact_repo=mock_repo)


async def _commit(service: FactInvalidationService, mock_db: AsyncMock) -> None:
    """Simulate the session committing.

    Invokes the queued ``after_commit`` hook directly (mirroring what the
    session event system calls) and yields control so the fire-and-forget
    task it schedules can complete.
    """
    service._on_after_commit(mock_db.sync_session)
    await asyncio.sleep(0)
    await asyncio.sleep(0)


class TestNormalizeIdentityTerm:
    """Scenario 4 — case/punctuation variants resolve to the same identity."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Alice", "alice"),
            ("  ALICE  ", "alice"),
            ("Nikita!", "nikita"),
            ("ExampleOrg, Inc.", "exampleorg inc"),
            ("O'Reilly", "oreilly"),
            ("", ""),
            (None, ""),
            ("FIEM College", "fiem college"),
        ],
    )
    def test_canonicalizes_terms(self, raw: str | None, expected: str) -> None:
        assert normalize_identity_term(raw) == expected

    def test_punctuation_variants_same_identity(self) -> None:
        """'Alice' vs 'Alice!' vs 'alice' all collapse to the same key."""
        identity = tuple(
            normalize_identity_term(t) for t in ("Alice!", "likes", "hiking?")
        )
        identity_plain = tuple(
            normalize_identity_term(t) for t in ("alice", "likes", "hiking")
        )
        assert identity == identity_plain


class TestSupersessionLogic:
    """Scenario 3 — identical content is skipped, never truncated."""

    @pytest.mark.asyncio
    async def test_identical_content_conflict_is_skipped(
        self, service: FactInvalidationService, mock_repo: AsyncMock
    ) -> None:
        """Re-ingesting the same SPO + content → skip, no set_valid_to."""
        candidate = _fact(content="Alice likes hiking")
        mock_repo.find_conflicting_active_for_update.return_value = [candidate]

        result = await service.ingest_with_supersession(
            org_id=ORG_ID,
            project_id=PROJECT_ID,
            user_id=USER_ID,
            facts=[_triple(content="Alice likes hiking")],
            now=NOW,
        )

        assert result.superseded_count == 0
        assert result.skipped_count == 1
        assert result.inserted_count == 0
        mock_repo.set_valid_to.assert_not_awaited()
        mock_repo.batch_create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_same_spo_different_content_supersedes(
        self, service: FactInvalidationService, mock_repo: AsyncMock
    ) -> None:
        """Different content for the same SPO → old truncated + new inserted."""
        candidate = _fact(content="Alice likes hiking")
        mock_repo.find_conflicting_active_for_update.return_value = [candidate]
        mock_repo.batch_create.return_value = [
            _fact(id=FACT_2_ID, content="Alice loves hiking")
        ]

        result = await service.ingest_with_supersession(
            org_id=ORG_ID,
            project_id=PROJECT_ID,
            user_id=USER_ID,
            facts=[_triple(content="Alice loves hiking")],
            now=NOW,
        )

        assert result.superseded_count == 1
        assert result.inserted_count == 1
        assert result.skipped_count == 0
        mock_repo.set_valid_to.assert_awaited_once_with(FACT_1_ID, NOW)
        mock_repo.batch_create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_punctuation_variant_conflicts_with_stored_fact(
        self, service: FactInvalidationService, mock_repo: AsyncMock
    ) -> None:
        """'Alice!' in a new batch conflicts with stored 'Alice' (same identity)."""
        candidate = _fact(subject="Alice", content="Alice likes hiking")
        mock_repo.find_conflicting_active_for_update.return_value = [candidate]
        mock_repo.batch_create.return_value = [
            _fact(id=FACT_2_ID, subject="Alice!", content="Alice! likes hiking")
        ]

        result = await service.ingest_with_supersession(
            org_id=ORG_ID,
            project_id=PROJECT_ID,
            user_id=USER_ID,
            facts=[_triple(subject="Alice!", content="Alice! likes hiking")],
            now=NOW,
        )

        assert result.superseded_count == 1
        mock_repo.set_valid_to.assert_awaited_once_with(FACT_1_ID, NOW)

    @pytest.mark.asyncio
    async def test_conflict_scan_is_org_scoped(
        self, service: FactInvalidationService, mock_repo: AsyncMock
    ) -> None:
        """Scenario 14 — the conflict scan receives the tenant + project scope."""
        mock_repo.batch_create.return_value = [
            _fact(id=FACT_2_ID, content="Alice likes hiking")
        ]

        await service.ingest_with_supersession(
            org_id=ORG_ID,
            project_id=PROJECT_ID,
            user_id=USER_ID,
            facts=[_triple()],
            now=NOW,
        )

        mock_repo.find_conflicting_active_for_update.assert_awaited_once()
        kwargs = mock_repo.find_conflicting_active_for_update.await_args.kwargs
        assert kwargs["org_id"] == ORG_ID
        assert kwargs["project_id"] == PROJECT_ID
        assert kwargs["now"] == NOW

    @pytest.mark.asyncio
    async def test_in_batch_duplicate_identity_deduped(
        self, service: FactInvalidationService, mock_repo: AsyncMock
    ) -> None:
        """Two identical (SPO, content) entries in one batch → 1 insert, 1 skip."""
        mock_repo.batch_create.return_value = [
            _fact(id=FACT_2_ID, content="Alice likes hiking")
        ]

        result = await service.ingest_with_supersession(
            org_id=ORG_ID,
            project_id=PROJECT_ID,
            user_id=USER_ID,
            facts=[
                _triple(content="Alice likes hiking"),
                _triple(content="Alice likes hiking"),
            ],
            now=NOW,
        )

        assert result.inserted_count == 1
        assert result.skipped_count == 1
        mock_repo.batch_create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_insert_failure_propagates(
        self, service: FactInvalidationService, mock_repo: AsyncMock
    ) -> None:
        """Scenario 7 — insert failure after truncate raises (caller rolls back).

        The truncate already ran (set_valid_to awaited) before the insert
        blew up.  Rollback is the caller's transaction concern — verified
        against real PG in the integration suite.
        """
        candidate = _fact(content="Alice likes hiking")
        mock_repo.find_conflicting_active_for_update.return_value = [candidate]
        mock_repo.batch_create.side_effect = RuntimeError("insert boom")

        with pytest.raises(RuntimeError, match="insert boom"):
            await service.ingest_with_supersession(
                org_id=ORG_ID,
                project_id=PROJECT_ID,
                user_id=USER_ID,
                facts=[_triple(content="Alice loves hiking")],
                now=NOW,
            )

        # The truncation happened before the failure — the caller's
        # rollback is what restores the old fact (integration-verified).
        mock_repo.set_valid_to.assert_awaited_once_with(FACT_1_ID, NOW)


class TestFormFlexibleSupersession:
    """Phase 2 ADR — form-flexible matching across identity forms.

    A string-identity fact and an entity-resolved fact of the same SPO
    names supersede each other (defect 1 + defect 3), while two distinct
    entities that merely share a name never conflict (entity precedence).
    """

    SUBJ_ENTITY = UUID("00000000-0000-0000-0000-00000000aaaa")
    OBJ_ENTITY = UUID("00000000-0000-0000-0000-00000000bbbb")
    OTHER_SUBJ = UUID("00000000-0000-0000-0000-00000000cccc")
    OTHER_OBJ = UUID("00000000-0000-0000-0000-00000000dddd")

    @pytest.mark.asyncio
    async def test_string_ingest_supersedes_entity_fact(
        self, service: FactInvalidationService, mock_repo: AsyncMock
    ) -> None:
        """Headline fix — an API (string-identity) fact supersedes an
        entity-linked fact with the same SPO names: latest assertion wins
        across forms (defect 1)."""
        candidate = _fact(
            subject="Robbie",
            predicate="wears",
            object="Adidas",
            content="Robbie wears Adidas",
            subject_entity_id=self.SUBJ_ENTITY,
            object_entity_id=self.OBJ_ENTITY,
        )
        mock_repo.find_conflicting_active_for_update.return_value = [candidate]
        mock_repo.batch_create.return_value = [
            _fact(id=FACT_2_ID, content="Robbie wears Adidas (confirmed)")
        ]

        result = await service.ingest_with_supersession(
            org_id=ORG_ID,
            project_id=PROJECT_ID,
            user_id=USER_ID,
            facts=[
                _triple(
                    subject="Robbie",
                    predicate="wears",
                    obj="Adidas",
                    content="Robbie wears Adidas (confirmed)",
                )
            ],
            now=NOW,
        )

        assert result.superseded_count == 1
        assert result.inserted_count == 1
        mock_repo.set_valid_to.assert_awaited_once_with(FACT_1_ID, NOW)

    @pytest.mark.asyncio
    async def test_entity_ingest_supersedes_literal_fact(
        self, service: FactInvalidationService, mock_repo: AsyncMock
    ) -> None:
        """Worker flip-flop — an entity-resolved ingest supersedes a literal
        fact with the same SPO names (defect 3, other direction)."""
        candidate = _fact(
            subject="Robbie", predicate="wears", object="Adidas"
        )  # literal — no entity IDs
        mock_repo.find_conflicting_active_for_update.return_value = [candidate]
        mock_repo.batch_create.return_value = [
            _fact(
                id=FACT_2_ID,
                subject_entity_id=self.SUBJ_ENTITY,
                object_entity_id=self.OBJ_ENTITY,
            )
        ]

        result = await service.ingest_with_supersession(
            org_id=ORG_ID,
            project_id=PROJECT_ID,
            user_id=USER_ID,
            facts=[
                _triple(
                    subject="Robbie",
                    predicate="wears",
                    obj="Adidas",
                    subject_entity_id=str(self.SUBJ_ENTITY),
                    object_entity_id=str(self.OBJ_ENTITY),
                )
            ],
            now=NOW,
        )

        assert result.superseded_count == 1
        mock_repo.set_valid_to.assert_awaited_once_with(FACT_1_ID, NOW)
        # The entity-form entry must ALSO scan with its SPO-string key —
        # a UUID-only match key would never return the literal candidate.
        match_keys = mock_repo.find_conflicting_active_for_update.await_args.kwargs[
            "match_keys"
        ]
        assert (self.SUBJ_ENTITY, "wears", self.OBJ_ENTITY) in match_keys
        assert ("Robbie", "wears", "Adidas") in match_keys

    @pytest.mark.asyncio
    async def test_distinct_entities_same_name_do_not_supersede(
        self, service: FactInvalidationService, mock_repo: AsyncMock
    ) -> None:
        """Entity precedence — both sides entity-linked with DIFFERENT UUIDs
        are distinct entities: identical names do NOT create a conflict."""
        candidate = _fact(
            subject="Robbie",
            predicate="wears",
            object="Adidas",
            subject_entity_id=self.SUBJ_ENTITY,
            object_entity_id=self.OBJ_ENTITY,
        )
        mock_repo.find_conflicting_active_for_update.return_value = [candidate]
        mock_repo.batch_create.return_value = [
            _fact(
                id=FACT_2_ID,
                subject_entity_id=self.OTHER_SUBJ,
                object_entity_id=self.OTHER_OBJ,
            )
        ]

        result = await service.ingest_with_supersession(
            org_id=ORG_ID,
            project_id=PROJECT_ID,
            user_id=USER_ID,
            facts=[
                _triple(
                    subject="Robbie",
                    predicate="wears",
                    obj="Adidas",
                    subject_entity_id=str(self.OTHER_SUBJ),
                    object_entity_id=str(self.OTHER_OBJ),
                )
            ],
            now=NOW,
        )

        assert result.superseded_count == 0
        assert result.inserted_count == 1
        mock_repo.set_valid_to.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_entity_and_literal_duplicates_collapse_in_batch(
        self, service: FactInvalidationService, mock_repo: AsyncMock
    ) -> None:
        """Entity + literal duplicates of ONE assertion in a batch collapse
        to a single row — not two coexisting rows (defect 3 observed)."""
        mock_repo.batch_create.return_value = [
            _fact(
                id=FACT_2_ID,
                subject_entity_id=self.SUBJ_ENTITY,
                object_entity_id=self.OBJ_ENTITY,
            )
        ]

        result = await service.ingest_with_supersession(
            org_id=ORG_ID,
            project_id=PROJECT_ID,
            user_id=USER_ID,
            facts=[
                _triple(
                    subject="Robbie",
                    predicate="wears",
                    obj="Adidas",
                    subject_entity_id=str(self.SUBJ_ENTITY),
                    object_entity_id=str(self.OBJ_ENTITY),
                    content="Robbie wears Adidas",
                ),
                _triple(
                    subject="Robbie",
                    predicate="wears",
                    obj="Adidas",
                    content="Robbie wears Adidas",
                ),
            ],
            now=NOW,
        )

        assert result.inserted_count == 1
        assert result.skipped_count == 1
        mock_repo.batch_create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_lock_key_identical_across_identity_forms(
        self, service: FactInvalidationService, mock_repo: AsyncMock
    ) -> None:
        """A string entry and an entity entry of the SAME triple produce the
        SAME advisory-lock key — cross-form writers serialize (defect 2)."""
        mock_repo.batch_create.return_value = [
            _fact(
                id=FACT_2_ID,
                subject_entity_id=self.SUBJ_ENTITY,
                object_entity_id=self.OBJ_ENTITY,
            )
        ]

        await service.ingest_with_supersession(
            org_id=ORG_ID,
            project_id=PROJECT_ID,
            user_id=USER_ID,
            facts=[
                _triple(
                    subject="Robbie",
                    predicate="wears",
                    obj="Adidas",
                    subject_entity_id=str(self.SUBJ_ENTITY),
                    object_entity_id=str(self.OBJ_ENTITY),
                ),
                _triple(
                    subject="Robbie",
                    predicate="wears",
                    obj="Adidas",
                    content="Robbie wears Adidas (raw)",
                ),
            ],
            now=NOW,
        )

        keys = mock_repo.lock_conflict_identities.await_args.args[0]
        assert keys == [f"sup:{ORG_ID}:{PROJECT_ID}:robbie:wears:adidas"]

    @pytest.mark.asyncio
    async def test_cross_form_punctuation_normalization(
        self, service: FactInvalidationService, mock_repo: AsyncMock
    ) -> None:
        """Case/punctuation normalization holds ACROSS forms: a string entry
        'Robbie! WEARS adidas' supersedes an entity fact stored 'Robbie'."""
        candidate = _fact(
            subject="Robbie",
            predicate="wears",
            object="Adidas",
            subject_entity_id=self.SUBJ_ENTITY,
            object_entity_id=self.OBJ_ENTITY,
        )
        mock_repo.find_conflicting_active_for_update.return_value = [candidate]
        mock_repo.batch_create.return_value = [_fact(id=FACT_2_ID)]

        result = await service.ingest_with_supersession(
            org_id=ORG_ID,
            project_id=PROJECT_ID,
            user_id=USER_ID,
            facts=[
                _triple(
                    subject="Robbie!",
                    predicate="WEARS",
                    obj="adidas",
                    content="Robbie wears Adidas",
                )
            ],
            now=NOW,
        )

        assert result.superseded_count == 1
        mock_repo.set_valid_to.assert_awaited_once_with(FACT_1_ID, NOW)


class TestAdvisoryLock:
    """B1 — concurrent same-SPO writers serialize on an advisory xact lock.

    The lock is taken per distinct conflict identity BEFORE the conflict
    scan so two writers whose scans both come up empty cannot both
    insert (the FOR UPDATE gap).  Keys are sorted for deadlock-free
    multi-key acquisition.
    """

    @pytest.mark.asyncio
    async def test_lock_acquired_per_identity_before_scan(
        self, service: FactInvalidationService, mock_repo: AsyncMock
    ) -> None:
        calls: list[str] = []

        async def _lock(keys: list[str]) -> None:
            calls.append("lock")

        async def _scan(**kwargs) -> list[SimpleNamespace]:
            calls.append("scan")
            return []

        mock_repo.lock_conflict_identities.side_effect = _lock
        mock_repo.find_conflicting_active_for_update.side_effect = _scan
        mock_repo.batch_create.return_value = [_fact(id=FACT_2_ID)]

        await service.ingest_with_supersession(
            org_id=ORG_ID,
            project_id=PROJECT_ID,
            user_id=USER_ID,
            facts=[
                _triple(),
                _triple(subject="Bob", predicate="leads", obj="Acme"),
            ],
            now=NOW,
        )

        assert calls == ["lock", "scan"], "locks must precede the conflict scan"
        mock_repo.lock_conflict_identities.assert_awaited_once()
        keys = mock_repo.lock_conflict_identities.await_args.args[0]
        assert len(keys) == 2
        assert keys == sorted(keys), "sorted acquisition prevents deadlock"
        assert any("alice" in k and "likes" in k and "hiking" in k for k in keys)
        assert any("bob" in k and "leads" in k and "acme" in k for k in keys)

    @pytest.mark.asyncio
    async def test_lock_key_scoped_by_org_and_project(
        self, service: FactInvalidationService, mock_repo: AsyncMock
    ) -> None:
        mock_repo.batch_create.return_value = [_fact(id=FACT_2_ID)]

        await service.ingest_with_supersession(
            org_id=ORG_ID,
            project_id=PROJECT_ID,
            user_id=USER_ID,
            facts=[_triple()],
            now=NOW,
        )
        (key,) = mock_repo.lock_conflict_identities.await_args.args[0]
        assert key.startswith(f"sup:{ORG_ID}:{PROJECT_ID}:")

    @pytest.mark.asyncio
    async def test_lock_key_always_uses_name_form_not_entity_uuids(
        self, service: FactInvalidationService, mock_repo: AsyncMock
    ) -> None:
        """Form-flexible fix — the advisory-lock key is the NAME form even
        for entity-resolved entries, so string and entity writers of the
        same triple serialize on the same lock.

        INTENTIONAL CHANGE: the previous version of this test pinned the
        form-sensitive key (``sup:...:<subject_uuid>:works_at:<object_uuid>``).
        That let a string writer and an entity writer of one triple take
        DIFFERENT locks — no cross-form serialization (defect 2)."""
        subj_entity = UUID("00000000-0000-0000-0000-00000000aaaa")
        obj_entity = UUID("00000000-0000-0000-0000-00000000bbbb")
        mock_repo.batch_create.return_value = [
            _fact(
                id=FACT_2_ID,
                subject_entity_id=subj_entity,
                object_entity_id=obj_entity,
            )
        ]

        await service.ingest_with_supersession(
            org_id=ORG_ID,
            project_id=PROJECT_ID,
            user_id=USER_ID,
            facts=[
                _triple(
                    subject="Alice",
                    predicate="works_at",
                    obj="Acme",
                    subject_entity_id=str(subj_entity),
                    object_entity_id=str(obj_entity),
                )
            ],
            now=NOW,
        )
        keys = mock_repo.lock_conflict_identities.await_args.args[0]
        # normalize_identity_term strips punctuation — including the
        # underscore in "works_at" — so the key is the canonical name form.
        assert keys == [f"sup:{ORG_ID}:{PROJECT_ID}:alice:worksat:acme"]

    @pytest.mark.asyncio
    async def test_in_batch_dedup_locks_identity_once(
        self, service: FactInvalidationService, mock_repo: AsyncMock
    ) -> None:
        """Two identical (identity, content) entries lock the identity once."""
        mock_repo.batch_create.return_value = [_fact(id=FACT_2_ID)]

        await service.ingest_with_supersession(
            org_id=ORG_ID,
            project_id=PROJECT_ID,
            user_id=USER_ID,
            facts=[_triple(), _triple()],
            now=NOW,
        )
        keys = mock_repo.lock_conflict_identities.await_args.args[0]
        assert len(keys) == 1

    @pytest.mark.asyncio
    async def test_no_lock_for_empty_batch(
        self, service: FactInvalidationService, mock_repo: AsyncMock
    ) -> None:
        await service.ingest_with_supersession(
            org_id=ORG_ID,
            project_id=PROJECT_ID,
            user_id=USER_ID,
            facts=[],
            now=NOW,
        )
        mock_repo.lock_conflict_identities.assert_not_awaited()


class TestSupersededWebhook:
    """Scenario 12 — FACT_SUPERSEDED webhook carries old/new fact IDs.

    M3: the emit is deferred until the caller's transaction commits —
    nothing fires while the transaction is still open.
    """

    @pytest.mark.asyncio
    async def test_webhook_emitted_after_commit_with_old_and_new_ids(
        self, mock_db: AsyncMock, mock_repo: AsyncMock
    ) -> None:
        webhook = AsyncMock()
        mock_repo.find_conflicting_active_for_update.return_value = [
            _fact(content="Alice likes hiking")
        ]
        mock_repo.batch_create.return_value = [
            _fact(id=FACT_2_ID, content="Alice loves hiking")
        ]
        service = FactInvalidationService(
            db=mock_db, fact_repo=mock_repo, webhook_service=webhook
        )

        await service.ingest_with_supersession(
            org_id=ORG_ID,
            project_id=PROJECT_ID,
            user_id=USER_ID,
            facts=[_triple(content="Alice loves hiking")],
            now=NOW,
        )

        # Pre-commit: no phantom event.
        webhook.emit.assert_not_awaited()

        await _commit(service, mock_db)

        webhook.emit.assert_awaited_once()
        _, kwargs = webhook.emit.await_args
        assert kwargs["organization_id"] == ORG_ID
        assert kwargs["event_type"] == EventType.FACT_SUPERSEDED
        assert kwargs["payload"]["old_fact_id"] == str(FACT_1_ID)
        assert kwargs["payload"]["new_fact_id"] == str(FACT_2_ID)
        assert kwargs["payload"]["project_id"] == str(PROJECT_ID)

    @pytest.mark.asyncio
    async def test_no_webhook_when_nothing_superseded(
        self, service: FactInvalidationService, mock_repo: AsyncMock
    ) -> None:
        mock_repo.batch_create.return_value = [_fact(id=FACT_2_ID)]

        result = await service.ingest_with_supersession(
            org_id=ORG_ID,
            project_id=PROJECT_ID,
            user_id=USER_ID,
            facts=[_triple()],
            now=NOW,
        )
        assert result.superseded_count == 0
        assert not service._pending_effects

    @pytest.mark.asyncio
    async def test_superseded_metric_incremented_after_commit(
        self, mock_db: AsyncMock, mock_repo: AsyncMock
    ) -> None:
        """openzync_facts_superseded_total counter reflects the batch — post-commit."""
        mock_repo.find_conflicting_active_for_update.return_value = [
            _fact(content="Alice likes hiking")
        ]
        mock_repo.batch_create.return_value = [
            _fact(id=FACT_2_ID, content="Alice loves hiking")
        ]
        service = FactInvalidationService(db=mock_db, fact_repo=mock_repo)

        with patch(
            "services.fact_invalidation_service.facts_superseded_total"
        ) as mock_counter:
            await service.ingest_with_supersession(
                org_id=ORG_ID,
                project_id=PROJECT_ID,
                user_id=USER_ID,
                facts=[_triple(content="Alice loves hiking")],
                now=NOW,
            )
            mock_counter.inc.assert_not_called()  # deferred, not committed yet

            await _commit(service, mock_db)
        mock_counter.inc.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_side_effects_dropped_on_rollback(
        self, mock_db: AsyncMock, mock_repo: AsyncMock
    ) -> None:
        """A rolled-back transaction must not emit webhooks or bump metrics."""
        webhook = AsyncMock()
        mock_repo.find_conflicting_active_for_update.return_value = [
            _fact(content="Alice likes hiking")
        ]
        mock_repo.batch_create.return_value = [
            _fact(id=FACT_2_ID, content="Alice loves hiking")
        ]
        service = FactInvalidationService(
            db=mock_db, fact_repo=mock_repo, webhook_service=webhook
        )
        await service.ingest_with_supersession(
            org_id=ORG_ID,
            project_id=PROJECT_ID,
            user_id=USER_ID,
            facts=[_triple(content="Alice loves hiking")],
            now=NOW,
        )
        assert service._pending_effects, "effects must be queued after ingest"

        service._on_after_rollback(mock_db.sync_session)
        assert not service._pending_effects, "rollback must drop queued effects"

        await _commit(service, mock_db)  # fires nothing — queue was cleared
        webhook.emit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_commit_hooks_attached_once_and_idempotent(
        self, mock_db: AsyncMock, mock_repo: AsyncMock
    ) -> None:
        """Hooks attach once; a commit with nothing queued is a no-op."""
        mock_repo.find_conflicting_active_for_update.return_value = [
            _fact(content="Alice likes hiking")
        ]
        mock_repo.batch_create.return_value = [
            _fact(id=FACT_2_ID, content="Alice loves hiking")
        ]
        service = FactInvalidationService(db=mock_db, fact_repo=mock_repo)

        await service.ingest_with_supersession(
            org_id=ORG_ID,
            project_id=PROJECT_ID,
            user_id=USER_ID,
            facts=[_triple(content="Alice loves hiking")],
            now=NOW,
        )
        assert event.contains(
            mock_db.sync_session, "after_commit", service._on_after_commit
        )
        assert event.contains(
            mock_db.sync_session, "after_rollback", service._on_after_rollback
        )

        await _commit(service, mock_db)

        # A later commit with nothing queued must not double-fire or raise.
        service._on_after_commit(mock_db.sync_session)
        await asyncio.sleep(0)
        assert not service._pending_effects

    @pytest.mark.asyncio
    async def test_entity_id_identity_used_when_present(
        self, service: FactInvalidationService, mock_repo: AsyncMock
    ) -> None:
        """Resolved entity UUIDs become the conflict identity (exact match)."""
        subj_entity = UUID("00000000-0000-0000-0000-00000000aaaa")
        obj_entity = UUID("00000000-0000-0000-0000-00000000bbbb")
        candidate = _fact(
            subject="Alice",
            predicate="works_at",
            object="Acme",
            content="Alice works at Acme",
            subject_entity_id=subj_entity,
            object_entity_id=obj_entity,
        )
        mock_repo.find_conflicting_active_for_update.return_value = [candidate]
        mock_repo.batch_create.return_value = [
            _fact(
                id=FACT_2_ID, subject_entity_id=subj_entity, object_entity_id=obj_entity
            )
        ]

        result = await service.ingest_with_supersession(
            org_id=ORG_ID,
            project_id=PROJECT_ID,
            user_id=USER_ID,
            facts=[
                _triple(
                    subject="Alice",
                    predicate="works_at",
                    obj="Acme",
                    subject_entity_id=str(subj_entity),
                    object_entity_id=str(obj_entity),
                )
            ],
            now=NOW,
        )

        assert result.superseded_count == 1
        # String match keys are passed through for the repo's UUID branch.
        match_keys = mock_repo.find_conflicting_active_for_update.await_args.kwargs[
            "match_keys"
        ]
        assert match_keys[0] == (subj_entity, "works_at", obj_entity)


class TestResultContract:
    """FactIngestionResult shape used by callers."""

    def test_defaults(self) -> None:
        result = FactIngestionResult()
        assert result.created == []
        assert result.inserted_count == 0
        assert result.superseded_count == 0
        assert result.skipped_count == 0


class TestGraphEdgeSync:
    """D1-rule edge expiry — the post-commit effect invokes the sync.

    The graph_sync collaborator receives the supersession events with
    edge keys; the post-commit effect computes the expire set and calls
    ``expire_relationships_matching`` on Postgres backends (case 2) or
    skips entirely (case 3 — successor re-asserts the same key).
    """

    SRC_ENTITY = UUID("00000000-0000-0000-0000-00000000aaaa")
    TGT_ENTITY = UUID("00000000-0000-0000-0000-00000000bbbb")
    OTHER_ENTITY = UUID("00000000-0000-0000-0000-00000000cccc")

    def _make_backend(self) -> Any:
        from unittest.mock import AsyncMock as _AsyncMock
        from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession

        from packages.graph_backend.postgres import PostgresGraphBackend

        backend = PostgresGraphBackend(db=_AsyncMock(spec=_AsyncSession))
        backend.expire_relationships_matching = _AsyncMock(return_value=1)
        return backend

    def _service_with_sync(
        self, mock_db: AsyncMock, mock_repo: AsyncMock, backend: Any
    ) -> FactInvalidationService:
        sync = GraphEdgeSyncService(backends=[backend])
        return FactInvalidationService(
            db=mock_db, fact_repo=mock_repo, graph_sync=sync
        )

    @pytest.mark.asyncio
    async def test_same_key_successor_skips_expiry(
        self, mock_db: AsyncMock, mock_repo: AsyncMock
    ) -> None:
        """Case 3 — successor re-asserts the same edge key: no expiry."""
        candidate = _fact(
            subject_entity_id=self.SRC_ENTITY,
            object_entity_id=self.TGT_ENTITY,
            predicate="works_at",
        )
        mock_repo.find_conflicting_active_for_update.return_value = [candidate]
        mock_repo.batch_create.return_value = [
            _fact(
                id=FACT_2_ID,
                subject_entity_id=self.SRC_ENTITY,
                object_entity_id=self.TGT_ENTITY,
                predicate="works_at",
            )
        ]
        backend = self._make_backend()
        service = self._service_with_sync(mock_db, mock_repo, backend)

        await service.ingest_with_supersession(
            org_id=ORG_ID,
            project_id=PROJECT_ID,
            user_id=USER_ID,
            facts=[
                _triple(
                    subject_entity_id=str(self.SRC_ENTITY),
                    predicate="works_at",
                    object_entity_id=str(self.TGT_ENTITY),
                )
            ],
            now=NOW,
        )
        # Pre-commit: nothing fired.
        backend.expire_relationships_matching.assert_not_awaited()

        await _commit(service, mock_db)

        # Case 3 — the edge survives, no expiry call.
        backend.expire_relationships_matching.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_literal_facts_never_expire(
        self, mock_db: AsyncMock, mock_repo: AsyncMock
    ) -> None:
        """Literal subject/object — no edge key, no expiry even on supersession."""
        candidate = _fact(content="Alice likes hiking")
        mock_repo.find_conflicting_active_for_update.return_value = [candidate]
        mock_repo.batch_create.return_value = [
            _fact(id=FACT_2_ID, content="Alice loves hiking")
        ]
        backend = self._make_backend()
        service = self._service_with_sync(mock_db, mock_repo, backend)

        await service.ingest_with_supersession(
            org_id=ORG_ID,
            project_id=PROJECT_ID,
            user_id=USER_ID,
            facts=[_triple(content="Alice loves hiking")],
            now=NOW,
        )
        await _commit(service, mock_db)
        backend.expire_relationships_matching.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_identity_mismatch_does_not_supersede(
        self, mock_db: AsyncMock, mock_repo: AsyncMock
    ) -> None:
        """Different edge keys at ingest are DIFFERENT identities — no event.

        The supersession conflict identity is the triple key itself
        (entity UUIDs when resolved): a fact asserting ``(SRC, pred,
        OTHER)`` does not supersede ``(SRC, pred, TGT)``, so no
        supersession event is recorded and nothing is expired.  The
        D1-rule case 2 (successor asserting a different key) is handled
        by the sync service for events that ARE recorded with differing
        keys — see ``test_graph_edge_sync_service.py``.
        """
        candidate = _fact(
            subject_entity_id=self.SRC_ENTITY,
            object_entity_id=self.TGT_ENTITY,
            predicate="works_at",
        )
        mock_repo.find_conflicting_active_for_update.return_value = [candidate]
        mock_repo.batch_create.return_value = [
            _fact(
                id=FACT_2_ID,
                subject_entity_id=self.SRC_ENTITY,
                object_entity_id=self.OTHER_ENTITY,
                predicate="works_at",
            )
        ]
        backend = self._make_backend()
        service = self._service_with_sync(mock_db, mock_repo, backend)

        result = await service.ingest_with_supersession(
            org_id=ORG_ID,
            project_id=PROJECT_ID,
            user_id=USER_ID,
            facts=[
                _triple(
                    subject_entity_id=str(self.SRC_ENTITY),
                    predicate="works_at",
                    object_entity_id=str(self.OTHER_ENTITY),
                )
            ],
            now=NOW,
        )
        assert result.superseded_count == 0
        await _commit(service, mock_db)
        backend.expire_relationships_matching.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_retraction_effect_expires_edge(
        self, mock_db: AsyncMock, mock_repo: AsyncMock
    ) -> None:
        """notify_retraction routes the D1 case-1 expiry through the same effect."""
        backend = self._make_backend()
        service = self._service_with_sync(mock_db, mock_repo, backend)

        old_fact = _fact(
            subject_entity_id=self.SRC_ENTITY,
            object_entity_id=self.TGT_ENTITY,
            predicate="works_at",
        )
        service.notify_retraction(
            org_id=ORG_ID, project_id=PROJECT_ID, old_fact=old_fact, at_time=NOW
        )
        backend.expire_relationships_matching.assert_not_awaited()

        await _commit(service, mock_db)

        backend.expire_relationships_matching.assert_awaited_once_with(
            org_id=ORG_ID,
            project_id=PROJECT_ID,
            source_id=self.SRC_ENTITY,
            target_id=self.TGT_ENTITY,
            relationship_type="works_at",
            at_time=NOW,
        )

    @pytest.mark.asyncio
    async def test_sync_effect_dropped_on_rollback(
        self, mock_db: AsyncMock, mock_repo: AsyncMock
    ) -> None:
        """A rolled-back transaction must not fire edge expiries either."""
        candidate = _fact(
            subject_entity_id=self.SRC_ENTITY,
            object_entity_id=self.TGT_ENTITY,
            predicate="works_at",
        )
        mock_repo.find_conflicting_active_for_update.return_value = [candidate]
        mock_repo.batch_create.return_value = [
            _fact(
                id=FACT_2_ID,
                subject_entity_id=self.SRC_ENTITY,
                object_entity_id=self.TGT_ENTITY,
                predicate="works_at",
            )
        ]
        backend = self._make_backend()
        service = self._service_with_sync(mock_db, mock_repo, backend)

        await service.ingest_with_supersession(
            org_id=ORG_ID,
            project_id=PROJECT_ID,
            user_id=USER_ID,
            facts=[
                _triple(
                    subject_entity_id=str(self.SRC_ENTITY),
                    predicate="works_at",
                    object_entity_id=str(self.TGT_ENTITY),
                )
            ],
            now=NOW,
        )
        assert service._pending_effects, "sync effect must be queued"

        service._on_after_rollback(mock_db.sync_session)
        await _commit(service, mock_db)
        backend.expire_relationships_matching.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_graph_sync_collaborator_is_noop(
        self, mock_db: AsyncMock, mock_repo: AsyncMock
    ) -> None:
        """Without a graph_sync collaborator the effect is never queued."""
        candidate = _fact(
            subject_entity_id=self.SRC_ENTITY,
            object_entity_id=self.TGT_ENTITY,
            predicate="works_at",
        )
        mock_repo.find_conflicting_active_for_update.return_value = [candidate]
        mock_repo.batch_create.return_value = [
            _fact(
                id=FACT_2_ID,
                subject_entity_id=self.SRC_ENTITY,
                object_entity_id=self.OTHER_ENTITY,
                predicate="works_at",
            )
        ]
        service = FactInvalidationService(db=mock_db, fact_repo=mock_repo)

        await service.ingest_with_supersession(
            org_id=ORG_ID,
            project_id=PROJECT_ID,
            user_id=USER_ID,
            facts=[
                _triple(
                    subject_entity_id=str(self.SRC_ENTITY),
                    predicate="works_at",
                    object_entity_id=str(self.OTHER_ENTITY),
                )
            ],
            now=NOW,
        )
        # Only the metric effect is queued — no graph sync effects.
        assert not any(
            "graph" in getattr(e, "__qualname__", "").lower()
            for e in service._pending_effects
        )
        await _commit(service, mock_db)
