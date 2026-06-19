"""OpenZep Python SDK — open-source agent memory platform.

Usage::

    from openzep import OpenZep

    client = OpenZep(api_key="mg_live_...")
    result = client.memory.ingest("project-id", messages=[
        {"role": "user", "content": "Hello world"},
    ])
    print(result.episode_count)
"""

from __future__ import annotations

from openzep.client import AsyncOpenZep, OpenZep

__all__ = [
    "AsyncOpenZep",
    "OpenZep",
]

# LangChain integration classes live under openzep.integrations.langchain.
# Import them with:
#   from openzep.integrations.langchain import OZChatMessageHistory, OZMemory, ...
# Requires: pip install openzep-py[langchain]

__version__ = "0.2.0"
