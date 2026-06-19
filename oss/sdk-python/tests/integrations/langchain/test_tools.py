"""Tests for LangChain tools."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from openzep.client import AsyncOpenZep
from openzep.integrations.langchain.tools.facts import AddFactsInput, AddFactsTool
from openzep.integrations.langchain.tools.graph import (
    GraphNodeDetailInput,
    GraphNodeDetailTool,
    GraphSearchInput,
    GraphSearchTool,
    ListGraphNodesInput,
    ListGraphNodesTool,
)


@pytest.fixture
def mock_client():
    """Create a real AsyncOpenZep with mocked sub-clients."""
    client = AsyncOpenZep(api_key="test", base_url="http://test")
    client.graph = AsyncMock()
    client.facts = AsyncMock()
    return client


class TestGraphSearchTool:
    """Tests for GraphSearchTool."""

    def test_name_and_description(self, mock_client):
        tool = GraphSearchTool(client=mock_client)
        assert tool.name == "graph_search"
        assert len(tool.description) > 0

    def test_args_schema(self, mock_client):
        tool = GraphSearchTool(client=mock_client)
        assert tool.args_schema == GraphSearchInput

    def test_input_schema_valid(self):
        inp = GraphSearchInput(query="test query", project_id="project-1")
        assert inp.query == "test query"
        assert inp.project_id == "project-1"

    def test_input_schema_with_optionals(self):
        inp = GraphSearchInput(
            query="test",
            project_id="project-1",
            types="facts",
            limit=10,
        )
        assert inp.types == "facts"
        assert inp.limit == 10

    @pytest.mark.asyncio
    async def test_arun_returns_formatted_results(self, mock_client):
        mock_client.graph.search.return_value = [
            {"content": "Alice works at Acme", "score": 0.95, "type": "fact"},
        ]

        tool = GraphSearchTool(client=mock_client)
        result = await tool._arun(query="Alice", project_id="project-1")

        assert "Alice works at Acme" in result
        assert "0.950" in result

    @pytest.mark.asyncio
    async def test_arun_empty_results(self, mock_client):
        mock_client.graph.search.return_value = []

        tool = GraphSearchTool(client=mock_client)
        result = await tool._arun(query="nothing", project_id="project-1")
        assert result == "No results found."


class TestGraphNodeDetailTool:
    """Tests for GraphNodeDetailTool."""

    def test_name_and_args_schema(self, mock_client):
        tool = GraphNodeDetailTool(client=mock_client)
        assert tool.name == "graph_node_detail"
        assert tool.args_schema == GraphNodeDetailInput


class TestAddFactsTool:
    """Tests for AddFactsTool."""

    def test_name_and_args_schema(self, mock_client):
        tool = AddFactsTool(client=mock_client)
        assert tool.name == "add_facts"
        assert tool.args_schema == AddFactsInput

    @pytest.mark.asyncio
    async def test_arun_accepts_facts(self, mock_client):
        mock_client.facts.add.return_value = AsyncMock(
            accepted_count=2, job_id="job-1"
        )

        tool = AddFactsTool(client=mock_client)
        facts = [
            {"subject": "Alice", "predicate": "works_at", "object": "Acme"},
            {"subject": "Alice", "predicate": "role", "object": "Engineer"},
        ]
        result = await tool._arun(project_id="project-1", facts=facts)

        assert "Accepted 2 fact(s)" in result
        assert "job-1" in result

    @pytest.mark.asyncio
    async def test_arun_calls_facts_add(self, mock_client):
        mock_client.facts.add.return_value = AsyncMock(
            accepted_count=1, job_id="job-1"
        )

        tool = AddFactsTool(client=mock_client)
        facts = [{"subject": "Alice", "predicate": "likes", "object": "Python"}]
        await tool._arun(project_id="project-1", facts=facts)

        # The tool normalizes dicts, adding content/confidence defaults
        mock_client.facts.add.assert_awaited_once()
        call_args = mock_client.facts.add.await_args
        assert call_args.args[0] == "project-1"
        expected = [
            {"subject": "Alice", "predicate": "likes", "object": "Python",
             "content": None, "confidence": 1.0},
        ]
        assert call_args.args[1] == expected


class TestListGraphNodesTool:
    """Tests for ListGraphNodesTool."""

    def test_name_and_args_schema(self, mock_client):
        tool = ListGraphNodesTool(client=mock_client)
        assert tool.name == "list_graph_nodes"
        assert tool.args_schema == ListGraphNodesInput
