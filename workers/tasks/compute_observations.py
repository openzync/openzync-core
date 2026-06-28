"""Placeholder for deferred graph-topology observations pass (bit 6).

Bit 6 of ``episodes.enrichment_status`` is reserved for a future worker
that runs deferred graph-topology observations (e.g., community detection,
centrality scoring, anomaly detection on the entity graph).

This worker is **not implemented** — the bit is reserved to prevent
collisions with other enrichment pipeline additions.  Implementation
will be scoped and scheduled separately.
"""

from __future__ import annotations
