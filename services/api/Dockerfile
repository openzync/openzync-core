# ──────────────────────────────────────────────────────────────────────────────
# OpenZync API — Production Docker image
# ──────────────────────────────────────────────────────────────────────────────
# Multi-stage build:
#   1. builder — installs runtime dependencies into ~/.local
#   2. runtime — minimal image that copies only the built artifacts + source
#
# Secret injection — OpenBao Agent sidecar pattern:
#   This container does NOT bake secrets into the image. A sibling sidecar
#   container (`openbao-agent-api`) authenticates to OpenBao via AppRole and
#   renders the system config (DATABASE_URL, SECRET_KEY, REDIS_URL, etc.) as
#   KEY=VALUE lines into /run/secrets/system.env on a shared tmpfs volume
#   (`api-secrets`). The api entrypoint script waits for that file, sources it,
#   then execs uvicorn. The api container therefore depends on the sidecar
#   being healthy AND having successfully rendered secrets before it can start.
#   Worker count is controlled via the UVICORN_WORKERS env var (sourced from
#   the rendered secrets, defaults to 1).
#
# Usage:
#   docker build -t openzync-api:latest -f services/api/Dockerfile .
#   docker run -p 8000:8000 openzync-api:latest
#   (with the openbao-agent-api sidecar + api-secrets tmpfs volume attached)
# ──────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim AS builder
WORKDIR /app

# Install build dependencies and the package itself.
COPY pyproject.toml .
RUN pip install --no-cache-dir --user -e .

# ──────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim AS runtime
WORKDIR /app

# Copy the entrypoint script — sources OpenBao Agent's rendered secrets,
# then execs uvicorn so it becomes PID 1 and receives signals directly.
# Copied before source so it lives in its own cache layer (rarely changes).
COPY scripts/entrypoint_api.sh /entrypoint_api.sh
RUN chmod +x /entrypoint_api.sh

# Copy installed packages from builder.
COPY --from=builder /root/.local /root/.local

# Copy application source.
COPY . .

# Ensure ~/.local/bin (where pip --user installs scripts) is on PATH.
ENV PATH=/root/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD wget --no-verbose --tries=1 --spider http://127.0.0.1:8000/v1/health || exit 1

# ENTRYPOINT runs the secret-wait + uvicorn-launch sequence as PID 1 (so the
# container receives SIGTERM directly for graceful shutdown). CMD is empty so
# docker-compose `command:` overrides pass through as args to the entrypoint.
# Worker count is controlled via the UVICORN_WORKERS env var rendered by the
# OpenBao Agent sidecar (see scripts/entrypoint_api.sh).
ENTRYPOINT ["/entrypoint_api.sh"]
CMD []
