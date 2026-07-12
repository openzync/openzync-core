"""Self-service endpoint for API-key-authenticated clients.

Provides a lightweight identity endpoint that returns the project_id
bound to the current API key — enabling SDK clients to auto-resolve
their project context without the caller passing it explicitly.
"""

from fastapi import APIRouter, Depends, Request

from dependencies.auth import require_org_id

router = APIRouter(prefix="/v1/api-key", tags=["API Key Self"])


@router.get("/project-id")
async def get_api_key_project_id(
    request: Request,
    _: None = Depends(require_org_id),
) -> dict:
    """Return the project_id scoped to the authenticating API key.

    For JWT-authenticated requests (dashboard sessions) this returns
    ``{"project_id": None}`` — dashboard users already know their
    project context through the regular API.

    For API-key-authenticated requests, the project_id is resolved
    from the key's project scope and cached by the SDK client.
    """
    return {
        "project_id": getattr(request.state, "api_key_project_id", None),
    }
