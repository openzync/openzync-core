"""Pydantic schemas for platform-level instance settings.

These describe the single-row ``instance_settings`` table and the
request/response shapes for ``PATCH /v1/platform/registration``.
The bootstrap token hash is never exposed — no schema references it.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RegistrationMode = Literal[
    "disabled",
    "enabled_with_org_code",
    "enabled_self_serve_org_creation",
]
"""Allowed registration modes for the platform."""

GraphBackendName = Literal["postgres", "falkordb", "surrealdb"]
"""Allowed default graph backends for newly created organizations."""


class DefaultBackends(BaseModel):
    """Default backend configuration applied to new organizations.

    Attributes:
        llm: Free-form LLM backend descriptor (``None`` = system default).
        graph: Graph backend name — one of ``postgres``, ``falkordb``,
            ``surrealdb``.
    """

    llm: dict[str, Any] | None = Field(
        default=None,
        description="LLM backend descriptor, or null for the system default.",
    )
    graph: GraphBackendName = Field(
        default="postgres",
        description="Default graph backend for new organizations.",
    )

    model_config = ConfigDict(extra="forbid")


class InstanceSettingsResponse(BaseModel):
    """Public view of instance settings (no token material).

    Attributes:
        registration_mode: Current registration mode.
        initialized: Whether the setup wizard has completed.
        default_backends: Defaults applied to wizard-created orgs.
    """

    registration_mode: RegistrationMode = Field(
        ...,
        description="Current registration mode.",
    )
    initialized: bool = Field(
        ...,
        description="Whether the instance setup wizard has completed.",
    )
    default_backends: DefaultBackends = Field(
        default_factory=DefaultBackends,
        description="Defaults applied to wizard-created organizations.",
    )

    model_config = ConfigDict(from_attributes=True)


class UpdateRegistrationModeRequest(BaseModel):
    """Request body for ``PATCH /v1/platform/registration``."""

    registration_mode: RegistrationMode = Field(
        ...,
        description="New registration mode for the platform.",
    )
