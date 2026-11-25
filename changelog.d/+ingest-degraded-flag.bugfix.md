Added `degraded`/`degrade_reason` to the ingest 202 response so ARQ-enqueue and blob-extraction failures are reported explicitly (reconciler replays the missing work) instead of succeeding silently.
