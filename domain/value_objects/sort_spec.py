"""Sort specification value object — validated sort request, no ORM."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SortDir = Literal["asc", "desc"]
"""Sort direction shared by every list endpoint."""


@dataclass(frozen=True, slots=True)
class SortSpec:
    """Validated sort request forwarded router → service → repository.

    Mirrors the ``core.sorting.SortSpec`` contract without importing
    framework-adjacent helpers, so domain code stays dependency-free.
    Repositories unpack this against their column whitelist and fail
    closed (422) on mismatch.

    Attributes:
        sort_by: Whitelisted snake_case column key, or ``None`` for the
            endpoint default.
        sort_dir: ``"asc"`` or ``"desc"``. Ignored when ``sort_by`` is
            ``None`` (the endpoint default direction applies).
    """

    sort_by: str | None = None
    sort_dir: SortDir = "asc"

    def effective(
        self, default_sort: str, default_dir: SortDir
    ) -> tuple[str, SortDir]:
        """Resolve the effective ``(sort_key, direction)``.

        Args:
            default_sort: Endpoint default preserving the current order.
            default_dir: Endpoint default direction.

        Returns:
            ``(sort_by, sort_dir)`` when a key was requested, else the
            endpoint defaults.
        """
        if self.sort_by is None:
            return default_sort, default_dir
        return self.sort_by, self.sort_dir
