Stop trusting `X-Forwarded-For` for the client IP recorded in audit log entries — the direct peer address is used, so a spoofed header can no longer forge the logged source.
