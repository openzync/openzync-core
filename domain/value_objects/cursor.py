"""Cursor value object — validated keyset-pagination position, no ORM."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Cursor:
    """A validated keyset-pagination position.

    The service layer decodes the opaque cursor (via ``core.cursor``) and
    hands the repository these resolved values — the repository never
    parses cursor strings itself.

    Attributes:
        sort_by: Sort key embedded in the cursor.
        sort_dir: Sort direction embedded in the cursor.
        value: Keyset position value (raw string; the repository coerces
            it to the column type).
        tiebreak_id: Row id tiebreak for stable pagination.
    """

    sort_by: str
    sort_dir: str
    value: str
    tiebreak_id: UUID

    def matches(self, sort_by: str, sort_dir: str) -> bool:
        """Check the cursor was minted for the requested sort.

        Args:
            sort_by: Requested sort key.
            sort_dir: Requested sort direction.

        Returns:
            True when both match the cursor's embedded sort.
        """
        return self.sort_by == sort_by and self.sort_dir == sort_dir

    def encode(self) -> str:
        """Encode this cursor to its opaque transport form.

        Returns:
            URL-safe base64 (no padding) of
            ``"sort_by:sort_dir:value|tiebreak_hex"``.
        """
        raw = f"{self.sort_by}:{self.sort_dir}:{self.value}|{self.tiebreak_id.hex}"
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(raw: str) -> Cursor:
    """Decode an opaque cursor into its resolved components.

    Args:
        raw: Opaque cursor string from a previous page response.

    Returns:
        The resolved :class:`Cursor`.

    Raises:
        ValueError: If the cursor is malformed or the sort prefix is
            missing. Callers map this to 422 via ``ValidationError``.
    """
    try:
        padding = 4 - len(raw) % 4
        padded = raw + ("=" * padding) if padding != 4 else raw
        decoded = base64.urlsafe_b64decode(padded.encode()).decode()
        prefix, _, id_hex = decoded.rpartition("|")
        sort_by, _, rest = prefix.partition(":")
        sort_dir, _, value = rest.partition(":")
        if not sort_by or sort_dir not in ("asc", "desc") or not id_hex:
            raise ValueError("missing sort prefix")
        return Cursor(
            sort_by=sort_by,
            sort_dir=sort_dir,
            value=value,
            tiebreak_id=UUID(hex=id_hex),
        )
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid cursor: {exc}") from exc
