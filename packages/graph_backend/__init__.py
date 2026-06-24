"""Graph backend abstraction — interface, PostgreSQL, SurrealDB, and FalkorDB implementations.

This package provides:
- ``GraphBackend`` — Abstract interface for graph-database operations
- ``PostgresGraphBackend`` — PostgreSQL-native implementation using
  recursive CTEs, ``pg_trgm``, and ``pgvector``.
- ``SurrealGraphBackend`` — SurrealDB implementation using native graph
  relations (``RELATE`` / arrow syntax), BM25 full-text search, and
  iterative BFS traversal.
- ``FalkorGraphBackend`` — FalkorDB-native implementation (RedisGraph
  module) using per-tenant graph keys, ``MERGE`` upserts, ``algo.bfs()``
  for GraphBLAS traversal, and RediSearch full-text search.

Usage::

    from packages.graph_backend import GraphBackend, SurrealGraphBackend
    from surrealdb import AsyncSurreal

    surreal = AsyncSurreal("ws://localhost:8000/rpc")
    await surreal.connect()
    await surreal.signin({"username": "root", "password": "root"})
    await surreal.use("openzep", "openzep")

    backend = SurrealGraphBackend(surreal=surreal)
    entity = await backend.create_entity(org_id=..., name="Acme", entity_type="company")
"""

from __future__ import annotations

from packages.graph_backend.falkordb import FalkorGraphBackend
from packages.graph_backend.interface import GraphBackend
from packages.graph_backend.postgres import PostgresGraphBackend
from packages.graph_backend.surrealdb import SurrealGraphBackend

__all__ = [
    "FalkorGraphBackend",
    "GraphBackend",
    "PostgresGraphBackend",
    "SurrealGraphBackend",
]
