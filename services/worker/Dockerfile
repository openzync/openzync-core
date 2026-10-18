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

# APP_VERSION carries the release version into builds without git history
# (build context excludes .git, so hatch-vcs would otherwise fall back).
ARG APP_VERSION=""

# Install the package with its [llm] extras, constrained to the CI-tested
# versions that broke production when a rebuild floated past them
# (constraints.txt pins only SQLAlchemy/asyncpg/pgvector; `-c` lets
# everything else resolve as before, so the installed set is unchanged).
# NOTE: README.md must be copied too — hatchling validates it exists at build time.
COPY constraints.txt pyproject.toml README.md .
RUN if [ -n "$APP_VERSION" ]; then export SETUPTOOLS_SCM_PRETEND_VERSION="$APP_VERSION"; fi \
    && pip install --no-cache-dir --user -c constraints.txt -e ".[llm]" \
    && pip install --no-cache-dir --user https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl

# Embeddings are served by the external Ollama service
# (nomic-embed-text:v1.5 via /api/embed) — no weights are baked into this
# image. The Ollama server address comes from the OpenBao system secret
# (OZ_OLLAMA_EMBED_URL).

# ──────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim AS runtime
WORKDIR /app

# Propagate the build-time version to runtime so core/_version.py prefers it.
ARG APP_VERSION=""
ENV OPENZYNC_VERSION=$APP_VERSION

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

# Worker exposes an aiohttp /health endpoint on settings.HEALTH_PORT (default 8081)
# managed by the ARQ worker loop. The 30s start-period covers the OpenBao Agent
# auth + render cycle; if /health is not up by then, the container is unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8081/health', timeout=3).status == 200 else 1)" || exit 1

# Entrypoint waits for /run/secrets/system.env (rendered by OpenBao Agent
# sidecar), sources it, then execs the ARQ worker. CMD is intentionally
# empty — all process selection lives in the entrypoint script.
ENTRYPOINT ["/entrypoint_worker.sh"]
CMD []
