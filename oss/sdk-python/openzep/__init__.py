"""OpenZep Python SDK — open-source agent memory platform.

Usage::

    from openzep import OpenZep

    client = OpenZep(api_key="mg_live_...")
    result = client.memory.ingest("user-id", messages=[
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

__version__ = "0.1.0"
