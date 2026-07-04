"""OpenZync Python SDK — open-source agent memory platform.

Usage::

    from openzync import OpenZync

    client = OpenZync(api_key="oz_live_...")
    result = client.memory.ingest("project-id", messages=[
        {"role": "user", "content": "Hello world"},
    ])
    print(result.episode_count)
"""

from __future__ import annotations

from openzync._version import __version__
from openzync.client import AsyncOpenZync, OpenZync

__all__ = [
    "AsyncOpenZync",
    "OpenZync",
]

# LangChain integration classes live under openzync.integrations.langchain.
# Import them with:
#   from openzync.integrations.langchain import OZChatMessageHistory, OZMemory, ...
# Requires: pip install openzync[langchain]
