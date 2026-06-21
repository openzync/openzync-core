"""Graph backend abstraction — interface and PostgreSQL implementation.

This package provides:
- ``GraphBackend`` — Abstract interface for graph-database operations
- ``PostgresGraphBackend`` — PostgreSQL-native implementation using
  recursive CTEs, ``pg_trgm``, and ``pgvector``.

Usage::

    from packages.graph_backend import GraphBackend, PostgresGraphBackend

    backend = PostgresGraphBackend(db_session)
    entity = await backend.create_entity(org_id, name="Acme", entity_type="company")
"""

from __future__ import annotations

from packages.graph_backend.interface import GraphBackend
from packages.graph_backend.postgres import PostgresGraphBackend

__all__ = [
    "GraphBackend",
    "PostgresGraphBackend",
]
