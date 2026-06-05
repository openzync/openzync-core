"""ASGI entry point for uvicorn.

Usage:
    uvicorn services.api.asgi:app --host 0.0.0.0 --port 8000

This module ensures the project root is on ``sys.path`` so that
``from core.*`` and ``from routers.*`` imports resolve correctly
when the application is run directly via ``uvicorn`` (as opposed to
being installed as a package).
"""

from __future__ import annotations

import os
import sys

# Insert the project root (two levels up from this file) so that
# `core/`, `routers/`, `models/`, etc. are importable without
# installing the package.
_project_root = os.path.join(os.path.dirname(__file__), "../..")
sys.path.insert(0, os.path.abspath(_project_root))

from services.api.main import app  # noqa: E402
