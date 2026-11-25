Made auth throttle counters atomic via a Lua INCR+EXPIRE script so concurrent first-hits can no longer orphan a counter without a TTL.
