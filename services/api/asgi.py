"""ASGI entry point for uvicorn.

Validates system configuration from ``OZ_*`` environment variables before
creating the FastAPI application.  If required env vars are missing the
process exits immediately — the container orchestrator (Docker/K8s)
handles restart.

Usage::

    uvicorn services.api.asgi:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import os
import sys

# Insert the project root (two levels up from this file) so that
# `core/`, `routers/`, `models/`, etc. are importable without
# installing the package.
_project_root = os.path.join(os.path.dirname(__file__), "../..")
sys.path.insert(0, os.path.abspath(_project_root))

from core.config import get_settings

# Fail fast at import time — uvicorn never starts without valid config.
get_settings()

from services.api.main import create_app  # noqa: E402

app = create_app()
