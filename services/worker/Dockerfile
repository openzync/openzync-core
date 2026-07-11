# ──────────────────────────────────────────────────────────────────────────────
# OpenZync Worker — Production Docker image
# ──────────────────────────────────────────────────────────────────────────────
# Multi-stage build:
#   1. builder — installs runtime dependencies into ~/.local
#   2. runtime — minimal image that copies only the built artifacts + source
#
# Secrets & config:
#   The worker depends on an OpenBao Agent sidecar that renders the system
#   config (DB URL, Redis URL, app secrets) to a tmpfs at
#   /openbao/agent/system.env. That tmpfs is bind-mounted into this container
#   at /run/secrets/system.env. The entrypoint script blocks until that file
#   exists, sources it, then execs the ARQ worker so the worker becomes PID 1
#   and receives signals directly (graceful shutdown on SIGTERM).
#
#   This container MUST NOT start until the OpenBao Agent sidecar has
#   authenticated and rendered /run/secrets/system.env. The entrypoint enforces
#   that contract; do not bypass it by running the CMD directly.
#
# Usage:
#   docker build -t openzync-worker:latest -f services/worker/Dockerfile .
#   docker run openzync-worker:latest
#   (the OpenBao Agent sidecar must be running and its tmpfs mounted in)
# ──────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim AS builder
WORKDIR /app

# Install build dependencies and the package itself.
COPY pyproject.toml .
RUN pip install --no-cache-dir --user -e .

# ──────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim AS runtime
WORKDIR /app

# Copy installed packages from builder.
COPY --from=builder /root/.local /root/.local

# Copy application source.
COPY . .

# Copy the entrypoint script — sources OpenBao Agent's rendered secrets,
# then execs the ARQ worker so it becomes PID 1 and receives signals directly.
COPY scripts/entrypoint_worker.sh /entrypoint_worker.sh
RUN chmod +x /entrypoint_worker.sh

# Ensure ~/.local/bin (where pip --user installs scripts) is on PATH.
ENV PATH=/root/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Worker exposes an aiohttp /health endpoint on settings.HEALTH_PORT (default 8080)
# managed by the ARQ worker loop. The 30s start-period covers the OpenBao Agent
# auth + render cycle; if /health is not up by then, the container is unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).status == 200 else 1)" || exit 1

# Entrypoint waits for /run/secrets/system.env (rendered by OpenBao Agent
# sidecar), sources it, then execs the ARQ worker. CMD is intentionally
# empty — all process selection lives in the entrypoint script.
ENTRYPOINT ["/entrypoint_worker.sh"]
CMD []
