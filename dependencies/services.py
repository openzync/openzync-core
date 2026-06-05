"""Service dependency factories.

This module provides dependency-injection factories for domain services.
Each factory retrieves the service's required dependencies (DB session, Redis
client, etc.) from ``request.app.state`` and returns an initialised service
instance.

Concrete service dependencies will be added in Phase 1.  For now, this is a
placeholder that demonstrates the pattern:

.. code-block:: python

    from functools import lru_cache

    from fastapi import Depends, Request
    from sqlalchemy.ext.asyncio import AsyncSession

    from dependencies.db import get_db


    # Example (uncomment in Phase 1):
    #
    # async def get_agent_service(
    #     request: Request,
    #     db: AsyncSession = Depends(get_db),
    # ) -> AgentService:
    #     \"\"\"Dependency that yields an initialised AgentService.\"\"\"
    #     redis = request.app.state.redis
    #     repo = AgentRepository(db)
    #     return AgentService(repo=repo, redis=redis)
"""

from __future__ import annotations

# Placeholder for Phase 1 service dependencies.
# Service factories will be added here as the domain expands.
