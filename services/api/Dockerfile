# ──────────────────────────────────────────────────────────────────────────────
# OpenZep API — Production Docker image
# ──────────────────────────────────────────────────────────────────────────────
# Multi-stage build:
#   1. builder — installs runtime dependencies into ~/.local
#   2. runtime — minimal image that copies only the built artifacts + source
#
# Usage:
#   docker build -t openzep-api:latest -f services/api/Dockerfile .
#   docker run -p 8000:8000 --env-file .env openzep-api:latest
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

# Ensure ~/.local/bin (where pip --user installs scripts) is on PATH.
ENV PATH=/root/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD curl -f http://localhost:8000/v1/health || exit 1

CMD ["uvicorn", "services.api.asgi:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
