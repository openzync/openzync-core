Sampled API-key `last_used_at` writes behind a Redis `auth:seen` guard with a fire-and-forget DB write, removing the per-request write from the auth hot path.
