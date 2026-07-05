# ──────────────────────────────────────────────────────────────────────────────
# OpenZync MCP Server — Production Docker image
# ──────────────────────────────────────────────────────────────────────────────
# Multi-stage build:
#   1. builder — installs runtime dependencies into ~/.local
#   2. runtime — minimal image that copies only the built artifacts + source
#
# Usage:
#   docker build -t openzync-mcp:latest -f services/mcp/Dockerfile .
#   docker run -p 8100:8100 --env-file .env openzync-mcp:latest
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

EXPOSE 8100

# The MCP server requires OPENZYN_API_KEY to be set at runtime.
# Default transport is stdio; override with --transport http --port 8100
# for HTTP transport, or --transport sse for SSE transport.
CMD ["python", "-m", "services.mcp", "--transport", "http", "--host", "0.0.0.0", "--port", "8100"]
