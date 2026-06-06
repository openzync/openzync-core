"""Community detection algorithms — Label Propagation on entity graphs.

Entities are read from PostgreSQL ``graph_entities`` and relationships
from ``graph_relationships``.  A ``networkx.Graph`` is built and Label
Propagation is run to detect clusters.
"""

from __future__ import annotations

from typing import Any

import networkx as nx
from networkx.algorithms.community import label_propagation_communities


def detect_communities_label_propagation(graph: nx.Graph) -> list[set[str]]:
    """Run Label Propagation community detection on an entity graph.

    Args:
        graph: NetworkX graph where nodes are entity UUID strings and edges
            represent relationships between entities.

    Returns:
        List of node sets, each set representing a community.
        Communities with fewer than 2 members are filtered out.
    """
    communities = list(label_propagation_communities(graph))
    return [c for c in communities if len(c) >= 2]


def build_entity_graph(
    entities: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
) -> nx.Graph:
    """Build a NetworkX graph from entities and relationships.

    Args:
        entities: List of entity dicts with at minimum ``id`` and ``name`` keys.
        relationships: List of relationship dicts with ``source_id``,
            ``target_id``, and ``relationship_type`` keys.

    Returns:
        A ``networkx.Graph`` with entity IDs as nodes and relationship types
        as edge attributes.
    """
    graph = nx.Graph()

    for entity in entities:
        graph.add_node(str(entity["id"]), name=entity.get("name", ""), type=entity.get("type", ""))

    for rel in relationships:
        source = str(rel.get("source_id", ""))
        target = str(rel.get("target_id", ""))
        if source and target and graph.has_node(source) and graph.has_node(target):
            graph.add_edge(
                source,
                target,
                type=rel.get("relationship_type", ""),
            )

    return graph
