"""LangChain integration for OpenZep.

Requires ``langchain-core``. Install with::

    pip install openzep-py[langchain]

Provides LangChain-native wrappers around OpenZep's memory, graph,
and fact APIs so they can be plugged into LangChain chains, agents,
and retrievers.
"""

from __future__ import annotations

from openzep.integrations.langchain.memory import OZMemory
from openzep.integrations.langchain.message_history import OZChatMessageHistory
from openzep.integrations.langchain.retriever import OZGraphRetriever
from openzep.integrations.langchain.tools.facts import AddFactsTool
from openzep.integrations.langchain.tools.graph import (
    GraphNodeDetailTool,
    GraphSearchTool,
    ListGraphNodesTool,
)

__all__ = [
    "OZChatMessageHistory",
    "OZMemory",
    "OZGraphRetriever",
    "GraphSearchTool",
    "GraphNodeDetailTool",
    "ListGraphNodesTool",
    "AddFactsTool",
]
