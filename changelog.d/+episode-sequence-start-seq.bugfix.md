Assign `sequence_number` as `start_seq + i` from a server-computed base in batch episode inserts, removing the in-batch-index fallback that restarted numbering at 0 on every batch.
