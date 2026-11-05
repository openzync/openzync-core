"""Fact conflict matching — identity normalization and supersession rules."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any
from uuid import UUID

from core.exceptions import ValidationError
from models.fact import Fact

logger = logging.getLogger(__name__)

# ── Conflict identity normalization ───────────────────────────────────────────
# Must stay byte-identical to the aggressive normalization in
# ``workers/tasks/extract_facts.py::_match_entity`` (step 5) so the
# extraction pipeline and every write path agree on what "same triple" means.
_ENTITY_NAME_RE = re.compile(r"[^a-z0-9\s]")


def normalize_identity_term(term: str | None) -> str:
    """Canonicalize a triple term for conflict-identity comparison.

    Args:
        term: Raw subject/predicate/object string, possibly ``None``.

    Returns:
        Lowercased term with punctuation stripped and surrounding
        whitespace removed.
    """
    if not term:
        return ""
    return _ENTITY_NAME_RE.sub("", term.lower()).strip()


def _parse_validity_bound(
    value: object, *, default: datetime | None
) -> datetime | None:
    """Coerce a validity-bound input to a ``datetime`` or ``None``.

    Extraction may pass validity bounds as ISO-8601 strings (LLM time
    extraction, Phase 1.3); ``datetime`` instances pass through.  A
    trailing ``Z`` (Zulu) marker is normalized to an explicit UTC offset
    before parsing.

    Args:
        value: Raw bound from the input fact dict.
        default: Value to return when ``value`` is ``None``.

    Returns:
        The parsed ``datetime``, or ``default`` for absent input.

    Raises:
        ValidationError: If ``value`` is neither ``None``, a
            ``datetime``, nor a parseable ISO-8601 string.
    """
    if value is None:
        return default
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        return datetime.fromisoformat(text)
    raise ValidationError(
        f"Invalid validity bound {value!r} — expected an ISO-8601 "
        "datetime string or a datetime."
    )


IdentityKey = tuple[UUID | str, str, UUID | str]
NameIdentity = tuple[str, str, str]
class ConflictMatchingMixin:
    """Composed into :class:`FactInvalidationService`."""

    _fact_repo: Any

    @staticmethod
    def _lock_key_for_identity(
        org_id: UUID, project_id: UUID, name_identity: NameIdentity
    ) -> str:
        """Derive the advisory-lock key for one conflict identity.

        The key ALWAYS uses the normalized NAME form — never entity
        UUIDs — so string-form and entity-form writers of the same
        triple serialize on the same lock (a UUID-keyed lock would let
        cross-form writers of one triple take different locks and
        coexist).  Distinct same-named entities false-serialize —
        acceptable, locks are brief and per-transaction.

        Args:
            org_id: Tenant scope.
            project_id: Project scope.
            name_identity: The normalized ``(subject, predicate, object)``
                name-form identity of the triple.

        Returns:
            A stable lock key for ``pg_advisory_xact_lock(hashtext(...))``.
        """
        subject, predicate, obj = name_identity
        return f"sup:{org_id}:{project_id}:{subject}:{predicate}:{obj}"
    def _prepare_entry(
        self,
        fact: dict[str, Any],
        source_episode_id: UUID | None,
        now: datetime,
    ) -> dict[str, Any]:
        """Normalize one input fact into a row + name identity + scan keys.

        Explicit ``valid_from``/``valid_to`` windows from the extraction
        pipeline are honored when present (Phase 1.3 time-based expiry,
        e.g. "sale ends Friday" → ``valid_to``); an absent ``valid_from``
        defaults to ``now``.  Born-dead ranges are rejected by the
        repository batch guards, not here — the service stays thin.
        """
        subject = str(fact.get("subject") or "").strip()
        predicate = str(fact.get("predicate") or "").strip()
        obj = str(fact.get("object") or "").strip()
        subject_entity_id = fact.get("subject_entity_id")
        object_entity_id = fact.get("object_entity_id")
        if isinstance(subject_entity_id, str):
            subject_entity_id = UUID(subject_entity_id) if subject_entity_id else None
        if isinstance(object_entity_id, str):
            object_entity_id = UUID(object_entity_id) if object_entity_id else None
        valid_from = _parse_validity_bound(fact.get("valid_from"), default=now)
        valid_to = _parse_validity_bound(fact.get("valid_to"), default=None)

        # The NAME form is the cross-form routing/dedup/lock key — every
        # writer of the same triple computes the same identity regardless
        # of whether it carries entity UUIDs.
        name_identity: NameIdentity = (
            normalize_identity_term(subject),
            normalize_identity_term(predicate),
            normalize_identity_term(obj),
        )
        # Scan keys: string-form entries scan by SPO text; entity-form
        # entries scan by UUID FIRST (preserves same-form supersession when
        # stored surface names drift from the raw text) AND by SPO text
        # (returns literal candidates for the cross-form match).
        match_keys: list[tuple[UUID | str, str, UUID | str]] = [
            (subject, predicate, obj)
        ]
        if subject_entity_id is not None and object_entity_id is not None:
            match_keys.insert(0, (subject_entity_id, predicate, object_entity_id))

        return {
            "name_identity": name_identity,
            "match_keys": match_keys,
            "triple": {"subject": subject, "predicate": predicate, "object": obj},
            "row": {
                "subject": subject,
                "predicate": predicate,
                "object": obj,
                "content": fact.get("content") or f"{subject} {predicate} {obj}",
                "confidence": float(fact.get("confidence", 1.0)),
                "subject_type": fact.get("subject_type", "literal"),
                "object_type": fact.get("object_type", "literal"),
                "subject_entity_id": subject_entity_id,
                "object_entity_id": object_entity_id,
                "source_episode_id": source_episode_id,
                "valid_from": valid_from,
                "valid_to": valid_to,
            },
        }

    @staticmethod
    def _identity_of_fact(fact: Fact) -> IdentityKey:
        """Compute the form-sensitive identity of a stored fact row.

        ``(subject_entity_id, predicate, object_entity_id)`` when both
        entity IDs resolve, else normalized ``(subject, predicate,
        object)`` strings.  Used for candidate classification inside
        :meth:`_candidate_conflicts` (entity form detected via the UUID
        components).
        """
        if fact.subject_entity_id is not None and fact.object_entity_id is not None:
            return (fact.subject_entity_id, fact.predicate, fact.object_entity_id)
        return (
            normalize_identity_term(fact.subject),
            normalize_identity_term(fact.predicate),
            normalize_identity_term(fact.object),
        )

    @staticmethod
    def _name_identity_of_fact(fact: Fact) -> NameIdentity:
        """Compute the NAME-form identity of a stored fact row.

        Normalized SPO strings — the cross-form grouping key: entity and
        literal rows of the same triple land in the same bucket so a
        string entry finds entity candidates and vice versa.

        Args:
            fact: A stored fact row.

        Returns:
            The normalized ``(subject, predicate, object)`` tuple.
        """
        # str() coercion keeps the grouping hashable for non-ORM stand-ins
        # (unit-test doubles); real Fact columns are NOT NULL strings, so
        # this is a no-op in production.
        def _term(value: object) -> str:
            return normalize_identity_term(str(value) if value is not None else "")

        return (
            _term(fact.subject),
            _term(fact.predicate),
            _term(fact.object),
        )

    @classmethod
    def _candidate_conflicts(cls, entry: dict[str, Any], candidate: Fact) -> bool:
        """Decide whether an incoming entry supersedes a candidate fact.

        Form-flexible matching (Phase 2 ADR intent):
        * **entity match** — the entry carries BOTH subject+object entity
          UUIDs, the candidate does too, they are equal, and predicates
          are equal → conflict.  Two entity-linked facts with DIFFERENT
          UUIDs are distinct entities even with identical names → no
          conflict (no name fallback once both sides resolve).
        * **name match** — normalized SPO strings are equal, applied
          whenever at least one side lacks the fully-resolved entity
          form → the cross-form path (string writer vs entity writer of
          the same triple).

        Args:
            entry: A prepared entry dict (see :meth:`_prepare_entry`).
            candidate: A stored fact row from the conflict scan.

        Returns:
            True when the entry must supersede the candidate.
        """
        row = entry["row"]
        entry_resolved = (
            row["subject_entity_id"] is not None
            and row["object_entity_id"] is not None
        )
        if entry_resolved:
            candidate_identity = cls._identity_of_fact(candidate)
            candidate_resolved = (
                isinstance(candidate_identity[0], UUID)
                and isinstance(candidate_identity[2], UUID)
            )
            if candidate_resolved:
                # Both resolved — entity match only.  Different UUIDs mean
                # distinct entities, however similar their names.
                return (
                    row["subject_entity_id"] == candidate_identity[0]
                    and row["predicate"] == candidate_identity[1]
                    and row["object_entity_id"] == candidate_identity[2]
                )
        # At least one side lacks the entity form — name match.
        return (
            normalize_identity_term(row["subject"])
            == normalize_identity_term(candidate.subject)
            and normalize_identity_term(row["predicate"])
            == normalize_identity_term(candidate.predicate)
            and normalize_identity_term(row["object"])
            == normalize_identity_term(candidate.object)
        )
