"""Shared response shaping for the admin metrics batch queries."""

from __future__ import annotations

ENRICHMENT_STATUS_LABELS: dict[int, str] = {
    0: "pending",
    1: "fact_extraction",
    3: "fact_extraction + classification",
    7: "fact_extraction + classification + summarization",
    15: "+ entity_links",
    31: "+ embedding",
    63: "fully_enriched",
}


def build_result(
    query_name: str,
    org_scoped: bool,
    columns: list[str],
    rows: list,
    params: dict,
    warning: str | None = None,
) -> dict:
    """Build the standard batch query response dict."""
    resp: dict = {
        "query": query_name,
        "org_scoped": org_scoped,
        "columns": columns,
        "rows": rows,
        "total": len(rows),
        "parameters": params,
    }
    if warning:
        resp["warning"] = warning
    return resp
