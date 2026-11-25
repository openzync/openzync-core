"""Graph backend abstraction — interface, SurrealDB, and FalkorDB implementations.

This package provides:
- ``GraphBackend`` — Abstract interface for graph-database operations
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
    await surreal.use("openzync", "openzync")

    backend = SurrealGraphBackend(surreal=surreal)
    entity = await backend.create_entity(user_id=..., name="Acme", entity_type="company")
"""

from __future__ import annotations

from packages.graph_backend.interface import GraphBackend

# FalkorDB and SurrealDB are optional backends — import them lazily
# from packages.graph_backend.falkordb import FalkorGraphBackend   # requires falkordb
# from packages.graph_backend.surrealdb import SurrealGraphBackend  # requires surrealdb

__all__ = [
    "GraphBackend",
]
