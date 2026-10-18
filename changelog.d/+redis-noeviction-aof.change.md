Set Redis to `noeviction` with AOF persistence enabled — DB 0 holds ARQ job and dedup keys alongside cache entries, so LRU eviction could silently drop queued work.
