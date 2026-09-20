"""Safe ORDER BY resolver — whitelist dict only, id ASC tiebreak always.

Repositories own a ``SORTABLE_COLUMNS: dict[str, InstrumentedAttribute]``
mapping whitelisted snake_case keys to ORM columns. This helper resolves
``(sort_by, sort_dir)`` to SQLAlchemy order clauses without ever calling
``text()``, f-strings, or ``getattr`` on raw input.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.sql.elements import UnaryExpression

SortDir = Literal["asc", "desc"]
"""Sort direction shared by every list endpoint."""


@dataclass(frozen=True, slots=True)
class SortSpec:
    """Validated sort request forwarded router → service → repository.

    Routers build this from ``sort_by``/``sort_dir`` Query params (already
    Literal-checked → 422 on unknown values). Services forward it opaque
    (zero SQLAlchemy in services). Repositories unpack it against their
    ``SORTABLE_COLUMNS`` whitelist via :func:`resolve_order_by` and fail
    closed (422) on mismatch.

    Attributes:
        sort_by: Whitelisted snake_case column key, or ``None`` for the
            endpoint default.
        sort_dir: ``"asc"`` or ``"desc"``. Ignored when ``sort_by`` is
            ``None`` (the endpoint default direction applies).
    """

    sort_by: str | None = None
    sort_dir: SortDir = "asc"

    def effective(self, default_sort: str, default_dir: SortDir) -> tuple[str, SortDir]:
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


def resolve_order_by(
    sortable: dict[str, Any],
    id_column: Any,
    sort_by: str | None,
    sort_dir: str,
    default_sort_by: str,
    default_dir: str = "desc",
) -> list[UnaryExpression[Any]]:
    """Resolve safe ORDER BY clauses from whitelisted keys.

    Args:
        sortable: Whitelist mapping ``sort_by`` key → ORM column.
        id_column: Primary-key column for the deterministic tiebreak.
        sort_by: Requested key, or ``None`` to use the default.
        sort_dir: ``"asc"`` or ``"desc"`` (already Literal-checked).
        default_sort_by: Key preserving the endpoint's current order.
        default_dir: Default direction preserving current order.

    Returns:
        ORDER BY clauses with ``id ASC`` tiebreak appended (unless the
        primary sort already is the id column).

    Raises:
        ValueError: On unknown ``sort_by`` or ``sort_dir`` (callers map
            to 422 via ``ValidationError``).
    """
    from core.exceptions import ValidationError

    key = sort_by if sort_by is not None else default_sort_by
    if key not in sortable:
        raise ValidationError(f"Invalid sort_by: {key!r}")
    if sort_dir not in ("asc", "desc"):
        raise ValidationError(f"Invalid sort_dir: {sort_dir!r}")
    if default_sort_by not in sortable:
        raise ValidationError(f"Invalid default sort: {default_sort_by!r}")
    if default_dir not in ("asc", "desc"):
        raise ValidationError(f"Invalid default dir: {default_dir!r}")

    # Effective direction: explicit sort_dir wins when sort_by given,
    # otherwise the endpoint default (preserves current orders byte-for-byte).
    effective_dir = sort_dir if sort_by is not None else default_dir
    column = sortable[key]
    primary = column.asc() if effective_dir == "asc" else column.desc()
    clauses: list[UnaryExpression[Any]] = [primary]
    if sortable.get(key) is not id_column and key != "id":
        clauses.append(id_column.asc())
    return clauses
