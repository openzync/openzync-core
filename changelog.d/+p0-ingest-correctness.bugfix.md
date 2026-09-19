Fixed message/episode cursor keyset predicate to use tuple comparison on `(sequence_number, id)`, eliminating duplicate/skipped rows at page boundaries.
Episode `sequence_number` is now server-assigned and contiguous via session `FOR UPDATE` + `MAX+1`, guarded by a partial unique index and backfill migration 0053 (duplicate sequence numbers eliminated; conflicts surface as 409 for client retry).
Blob extraction failures are now fail-closed (raise + ARQ retry, success bit unset) instead of being marked successful.
Content dedup is atomic via a Lua GET-or-SET claim: concurrent identical ingests replay the winner `job_id` and write a single row.
