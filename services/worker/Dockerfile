# ──────────────────────────────────────────────────────────────────────────────
# OpenZep Worker — Production Docker image
# ──────────────────────────────────────────────────────────────────────────────
# Multi-stage build for the ARQ background worker.
#
# Usage:
#   docker build -t openzep-worker:latest -f services/worker/Dockerfile .
#   docker run --env-file .env openzep-worker:latest
# ──────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim AS builder
WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir --user -e .

# ──────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim AS runtime
WORKDIR /app

COPY --from=builder /root/.local /root/.local
COPY . .

ENV PATH=/root/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import redis; r=redis.from_url('$REDIS_URL'); r.ping(); print('ok')" || exit 1

CMD ["python", "-m", "services.worker.worker"]
