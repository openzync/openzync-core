Fail closed with 503 when Redis is unreachable for the auth throttle and negative cache, instead of falling through to unthrottled Postgres on every request.
