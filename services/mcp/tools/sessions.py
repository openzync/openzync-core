"""Session tool handler — list_sessions."""

from __future__ import annotations

from openzep.client import AsyncOpenZep


async def handle_list_sessions(client: AsyncOpenZep, args: dict) -> dict:
    """List sessions for a user."""
    user_id = args["user_id"]
    limit = args.get("limit", 50)

    result = await client.sessions.list(user_id=user_id, limit=limit)
    sessions = result.get("data", result.get("items", []))

    if not sessions:
        return {
            "content": [{"type": "text", "text": "No sessions found."}],
            "isError": False,
        }

    lines = [f"Found {len(sessions)} session(s):"]
    for s in sessions:
        sid = s.get("id", "")[:8]
        ext = s.get("external_id", "")
        msgs = s.get("message_count", 0)
        lines.append(f"  [{sid}] {ext} ({msgs} messages)")

    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "isError": False,
    }
