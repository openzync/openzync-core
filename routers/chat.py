"""Chat endpoint — SSE-streamed LLM conversation with MCP tool access.

Single endpoint:
    POST /v1/users/{user_id}/chat
    → SSE stream of tokens, tool calls, and tool results.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from dependencies.auth import require_org_id
from dependencies.services import get_chat_service
from schemas.chat import ChatRequest
from services.chat_service import ChatService

logger = logging.getLogger("openzep.routers.chat")

router = APIRouter(
    prefix="/v1/users/{user_id}/chat",
    tags=["Chat"],
)


@router.post(
    "",
    response_class=StreamingResponse,
    summary="Chat with the AI assistant",
    description="Send a message and receive an SSE stream of tokens, "
    "tool calls, and results.  The LLM has access to the full OpenZep "
    "API through MCP tools.",
    responses={
        200: {"description": "SSE event stream."},
        401: {"description": "Missing or invalid authentication."},
    },
)
async def chat_stream(
    user_id: UUID,
    body: ChatRequest,
    service: ChatService = Depends(get_chat_service),
    org_id: str = Depends(require_org_id),
):
    """Chat SSE endpoint.

    Streams SSE events: message_stored, tool_call, tool_result,
    start, token, error, done.
    """
    org_uuid = UUID(org_id) if isinstance(org_id, str) else org_id

    # Resolve session — create or reuse __chat__ session if not provided
    session_id = body.session_id
    if session_id is None:
        session_id = await service.get_or_create_chat_session(
            user_id=user_id,
            org_id=org_uuid,
        )

    return StreamingResponse(
        service.chat(
            user_id=user_id,
            org_id=org_uuid,
            session_id=session_id,
            message=body.message,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
