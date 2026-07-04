"""LangChain integration for OpenZync.

Requires ``langchain-core``. Install with::

    pip install openzync[langchain]

Provides LangChain-native wrappers around OpenZync's memory, graph,
and fact APIs so they can be plugged into LangChain chains, agents,
and retrievers.
"""

from __future__ import annotations

from openzync.integrations.langchain.memory import OZMemory
from openzync.integrations.langchain.message_history import OZChatMessageHistory
from openzync.integrations.langchain.retriever import OZGraphRetriever
from openzync.integrations.langchain.tools.facts import AddFactsTool
from openzync.integrations.langchain.tools.graph import (
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
