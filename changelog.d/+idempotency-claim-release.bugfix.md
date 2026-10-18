Release the content-hash idempotency claim when an ingest fails, so a retry is no longer rejected as a duplicate of a write that never completed.
