"""OpenZep Python SDK — open-source agent memory platform.

Usage::

    from openzync import OpenZep

    client = OpenZep(api_key="mg_live_...")
    result = client.memory.ingest("project-id", messages=[
        {"role": "user", "content": "Hello world"},
    ])
    print(result.episode_count)
"""

from __future__ import annotations

from openzync._version import __version__
from openzync.client import AsyncOpenZep, OpenZep

__all__ = [
    "AsyncOpenZep",
    "OpenZep",
]

# LangChain integration classes live under openzync.integrations.langchain.
# Import them with:
#   from openzync.integrations.langchain import OZChatMessageHistory, OZMemory, ...
# Requires: pip install openzync[langchain]
