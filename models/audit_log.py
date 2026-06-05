"""Audit log model — immutable, append-only record of security-relevant events.

Every API call, authentication event, and administrative action is logged here.
The table is **immutable**: no UPDATE or DELETE is permitted at the application
layer. This model intentionally omits ``updated_at``.
"""

import uuid

from sqlalchemy import CheckConstraint, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, CreatedAtMixin


class AuditLog(CreatedAtMixin, Base):
    """An immutable audit trail entry.

    Attributes:
        id: UUID primary key.
        organization_id: Optional — may be null for unauthenticated actions.
        actor_id: Identifier of the acting entity (user ID, API key prefix,
            or system name).
        actor_type: Type of actor — one of ``user``, ``api_key``, ``system``.
        action: The action performed (e.g., ``session.create``,
            ``api_key.revoke``).
        resource_type: Type of resource affected (e.g., ``session``, ``fact``).
        resource_id: Identifier of the affected resource (nullable for
            collection-level actions).
        details: Arbitrary JSONB payload with action-specific context.
        ip_address: Source IP address of the request.
        created_at: Immutable timestamp of the event (inherited from
            ``CreatedAtMixin``).
    """

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    actor_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_type: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    resource_type: Mapped[str] = mapped_column(Text, nullable=False)
    resource_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    ip_address: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "actor_type IN ('user', 'api_key', 'system')",
            name="ck_audit_log_actor_type",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<AuditLog id={self.id} action={self.action!r} "
            f"actor={self.actor_id!r} type={self.actor_type!r}>"
        )
