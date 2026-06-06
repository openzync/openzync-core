"""Tests for community detection algorithms."""

from __future__ import annotations

import pytest
import networkx as nx

from packages.community.algorithms import (
    build_entity_graph,
    detect_communities_label_propagation,
)


class TestBuildEntityGraph:
    """Tests for ``build_entity_graph``."""

    def test_empty_graph(self):
        graph = build_entity_graph([], [])
        assert graph.number_of_nodes() == 0
        assert graph.number_of_edges() == 0

    def test_basic_graph(self):
        entities = [
            {"id": "1", "name": "Alice", "type": "Person"},
            {"id": "2", "name": "Acme Corp", "type": "Organization"},
            {"id": "3", "name": "Bob", "type": "Person"},
        ]
        relationships = [
            {"source_id": "1", "target_id": "2", "relationship_type": "works_at"},
            {"source_id": "3", "target_id": "2", "relationship_type": "works_at"},
        ]

        graph = build_entity_graph(entities, relationships)
        assert graph.number_of_nodes() == 3
        assert graph.number_of_edges() == 2
        assert graph.has_edge("1", "2")
        assert graph.has_edge("3", "2")

    def test_skips_missing_entities(self):
        entities = [{"id": "1", "name": "Alice", "type": "Person"}]
        relationships = [
            {"source_id": "1", "target_id": "999", "relationship_type": "works_at"},
        ]

        graph = build_entity_graph(entities, relationships)
        assert graph.number_of_nodes() == 1
        assert graph.number_of_edges() == 0

    def test_edge_attributes(self):
        entities = [
            {"id": "1", "name": "Alice", "type": "Person"},
            {"id": "2", "name": "Acme", "type": "Org"},
        ]
        relationships = [
            {"source_id": "1", "target_id": "2", "relationship_type": "works_at"},
        ]

        graph = build_entity_graph(entities, relationships)
        assert graph.edges[("1", "2")]["type"] == "works_at"


class TestDetectCommunities:
    """Tests for ``detect_communities_label_propagation``."""

    def test_single_community(self):
        graph = nx.Graph()
        graph.add_edges_from([("1", "2"), ("2", "3"), ("3", "1")])

        communities = detect_communities_label_propagation(graph)
        assert len(communities) >= 1

    def test_two_communities(self):
        graph = nx.Graph()
        # Community A: 1-2-3
        graph.add_edges_from([("1", "2"), ("2", "3")])
        # Community B: 4-5-6
        graph.add_edges_from([("4", "5"), ("5", "6")])

        communities = detect_communities_label_propagation(graph)
        assert len(communities) >= 2

    def test_filters_singletons(self):
        graph = nx.Graph()
        graph.add_node("1")
        graph.add_edge("2", "3")

        communities = detect_communities_label_propagation(graph)
        for c in communities:
            assert len(c) >= 2
