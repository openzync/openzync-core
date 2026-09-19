Message/episode listing now uses the v1 cursor envelope: legacy or malformed cursors are rejected with 400 `cursor_expired` (restart from page 1), and `get_by_project_id` ordering is aligned to `(sequence_number, id)` to match the keyset predicate.
PII redaction is now fail-closed: an OpenBao/redaction outage returns 503 `pii_unavailable` with `Retry-After: 30` instead of persisting unredacted content.
The legacy `quotas -> pii` config fallback is removed — orgs still carrying quota-stored PII config must migrate to the dedicated PII store before upgrading.
