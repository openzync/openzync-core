"""Graphiti client abstraction — graph-database interface and FalkorDB backend.

This package provides a ``GraphBackend`` abstract interface that decouples
OpenZep's memory layer from any specific graph database technology, along
with a concrete ``FalkorDBBackend`` implementation powered by Graphiti.

Usage::

    from packages.graphiti_client import FalkorDBBackend, GraphBackend

    backend: GraphBackend = FalkorDBBackend(graphiti_client)
    entity = await backend.create_entity(org_id, name="Alice", entity_type="person")
"""

from __future__ import annotations

from packages.graphiti_client.backends.falkordb import FalkorDBBackend
from packages.graphiti_client.interface import GraphBackend

__all__ = [
    "FalkorDBBackend",
    "GraphBackend",
]
