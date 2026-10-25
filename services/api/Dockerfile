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

FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704 AS builder
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

FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704 AS runtime
WORKDIR /app

# Run as non-root: appuser owns /app; installed deps copy to its $HOME.
RUN groupadd -r appuser && useradd -r -g appuser -d /home/appuser -m appuser \
    && chown appuser:appuser /app

# Propagate the build-time version to runtime so core/_version.py prefers it.
ARG APP_VERSION=""
ENV OPENZYNC_VERSION=$APP_VERSION

# Copy the entrypoint script — sources OpenBao Agent's rendered secrets,
# then execs uvicorn so it becomes PID 1 and receives signals directly.
# Copied before source so it lives in its own cache layer (rarely changes).
COPY scripts/entrypoint_api.sh /entrypoint_api.sh
RUN chmod +x /entrypoint_api.sh

# Copy installed packages from builder (into appuser's $HOME for pip --user layout).
COPY --from=builder --chown=appuser:appuser /root/.local /home/appuser/.local

# Copy application source.
COPY --chown=appuser:appuser . .

# Ensure ~/.local/bin (where pip --user installs scripts) is on PATH.
ENV PATH=/home/appuser/.local/bin:$PATH \
    HOME=/home/appuser \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1

# ENTRYPOINT runs the secret-wait + uvicorn-launch sequence as PID 1 (so the
# container receives SIGTERM directly for graceful shutdown). CMD is empty so
# docker-compose `command:` overrides pass through as args to the entrypoint.
# Worker count is controlled via the UVICORN_WORKERS env var rendered by the
# OpenBao Agent sidecar (see scripts/entrypoint_api.sh).
ENTRYPOINT ["/entrypoint_api.sh"]
CMD []
