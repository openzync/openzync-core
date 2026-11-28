"""Claimed invite value object — identity of a successfully claimed invite."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ClaimedInvite:
    """Identity of a successfully claimed invite.

    Returned by the invite-claim write — the fields the accept flow
    needs to issue tokens, read back via ``RETURNING`` so no second
    round-trip (and no stale identity-mapped row) is involved.
    """

    id: UUID
    role: str
