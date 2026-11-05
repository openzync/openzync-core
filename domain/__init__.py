"""Pure domain layer — framework-free business entities and contracts.

Every module under ``domain/`` uses only the standard library. Pydantic,
SQLAlchemy, and FastAPI imports are forbidden here; repositories map ORM
rows to these dataclasses at the boundary.
"""

from __future__ import annotations
