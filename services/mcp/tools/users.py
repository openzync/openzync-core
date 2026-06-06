"""User tool handler — create_user."""

from __future__ import annotations

from openzep.client import AsyncOpenZep


async def handle_create_user(client: AsyncOpenZep, args: dict) -> dict:
    """Create a new user."""
    external_id = args["external_id"]
    name = args.get("name")

    user = await client.users.create(external_id=external_id, name=name)

    return {
        "content": [
            {
                "type": "text",
                "text": (
                    f"User created successfully.\n"
                    f"  ID: {user.id}\n"
                    f"  External ID: {user.external_id}\n"
                    f"  Name: {user.name}"
                ),
            }
        ],
        "isError": False,
    }
