Remove the worker's custom SIGTERM/SIGINT handlers and let ARQ own signal handling, restoring graceful shutdown and in-flight job draining.
