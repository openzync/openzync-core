Fixed memory wipe losing files when the DB commit failed; wipe now soft-deletes rows, commits, then runs a best-effort S3 sweep, with orphan GC backfilling any leftovers.
