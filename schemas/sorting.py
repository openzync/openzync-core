"""Shared sorting contract — SortDir + per-resource SortBy Literals.

Routers import these Literals for ``Query`` params so unknown values
fail with 422 before reaching the service layer. Services forward a
:class:`core.sorting.SortSpec` untouched (zero SQLAlchemy in services).
Repositories own ``SORTABLE_COLUMNS`` dicts mapping these keys to ORM
columns and call :func:`core.sorting.resolve_order_by` for safe ordering.

``SortSpec`` is re-exported here so the HTTP contract (this module) and
the forwarding unit stay importable from one place; the canonical
definition lives in :mod:`core.sorting` so repositories can use it
without importing from ``schemas`` (DDD layering).
"""

from __future__ import annotations

from typing import Literal

from core.sorting import SortDir, SortSpec

__all__ = [
    "SortDir",
    "SortSpec",
    "UserSortBy",
    "OrgMemberSortBy",
    "ProjectSortBy",
    "ProjectMemberSortBy",
    "SessionSortBy",
    "SessionFactSortBy",
    "ProjectFactSortBy",
    "FactHistorySortBy",
    "AuditLogSortBy",
    "ApiKeySortBy",
    "GraphNodeSortBy",
    "GraphEdgeSortBy",
    "CommunitySortBy",
    "ObservationSortBy",
    "MessageSortBy",
    "ExtractionSortBy",
    "ClassificationSortBy",
    "AdminSchemaSortBy",
    "WebhookSortBy",
    "PromptSortBy",
    "OrgSortBy",
    "MonitorTargetSortBy",
    "SearchSort",
]

UserSortBy = Literal["external_id", "name", "email", "created_at"]
OrgMemberSortBy = Literal["created_at", "name", "email"]
ProjectSortBy = Literal["name", "created_at", "updated_at", "pinned_at"]
ProjectMemberSortBy = Literal["created_at", "role"]
SessionSortBy = Literal["external_id", "created_at", "updated_at"]
SessionFactSortBy = Literal["created_at", "confidence", "subject"]
ProjectFactSortBy = Literal["valid_from", "created_at", "confidence", "subject"]
FactHistorySortBy = Literal["at_time"]
AuditLogSortBy = Literal["created_at", "action", "status_code", "actor_id"]
ApiKeySortBy = Literal["name", "created_at", "last_used_at"]
GraphNodeSortBy = Literal["name", "created_at", "entity_type"]
GraphEdgeSortBy = Literal["created_at", "predicate"]
CommunitySortBy = Literal["name", "created_at", "member_count"]
ObservationSortBy = Literal["name", "created_at"]
MessageSortBy = Literal["sequence_number", "created_at"]
ExtractionSortBy = Literal["sequence_number", "created_at"]
ClassificationSortBy = Literal["sequence_number", "created_at"]
AdminSchemaSortBy = Literal["name", "created_at"]
WebhookSortBy = Literal["name", "created_at"]
PromptSortBy = Literal["name", "created_at"]
OrgSortBy = Literal["name", "created_at"]
MonitorTargetSortBy = Literal["name", "created_at", "type", "status"]
SearchSort = Literal["relevance", "recent"]
