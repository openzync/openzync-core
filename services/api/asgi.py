"""ASGI entry point for uvicorn.

Bootstraps OpenBao before creating the FastAPI application.  If OpenBao is
unreachable the process exits immediately — the container orchestrator
(Docker/K8s) handles restart.

Usage::

    uvicorn services.api.asgi:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import os
import sys

# Insert the project root (two levels up from this file) so that
# `core/`, `routers/`, `models/`, etc. are importable without
# installing the package.
_project_root = os.path.join(os.path.dirname(__file__), "../..")
sys.path.insert(0, os.path.abspath(_project_root))

from core.config import BootstrapSettings
from core.openbao import OpenBaoClient
from core.openbao_settings import init_settings


async def _bootstrap() -> None:
    """Connect to OpenBao and load all system settings.

    Fail-fast: raises OpenBaoConnectionError if OpenBao is unreachable.
    """
    bootstrap = BootstrapSettings()
    async with OpenBaoClient(
        bootstrap.OPENBAO_ADDR,
        bootstrap.OPENBAO_ROLE_ID,
        bootstrap.OPENBAO_SECRET_ID,
        timeout=15.0,
    ) as bao:
        await init_settings(bao)


# Fail fast at import time — uvicorn never starts without OpenBao
asyncio.run(_bootstrap())

from services.api.main import create_app  # noqa: E402

app = create_app()
