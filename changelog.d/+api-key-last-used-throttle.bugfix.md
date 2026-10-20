Make the API key `last_used_at` write a single conditional UPDATE (only when the stored value is older than five minutes) instead of an unconditional write on every authenticated request.
