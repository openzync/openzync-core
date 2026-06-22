# ──────────────────────────────────────────────────────────────────────────────
# OpenZep Frontend — Production Docker image
# ──────────────────────────────────────────────────────────────────────────────
# Multi-stage build:
#   1. builder — installs dependencies + builds the Next.js app
#   2. runner  — minimal image with standalone server + assets only
#
# Usage:
#   docker build -t openzep-frontend:latest -f frontend/Dockerfile frontend/
#
# The frontend expects a reverse proxy (nginx) to route /v1/* requests
# to the API.  NEXT_PUBLIC_API_URL defaults to "" (relative URLs), so
# the frontend makes requests like /v1/sessions that reach the API
# through the shared nginx origin.
# ──────────────────────────────────────────────────────────────────────────────

# ── Stage 1: Build ────────────────────────────────────────────────────────────
FROM node:20-alpine AS builder
WORKDIR /app

# Install ALL dependencies (dev deps needed for build).
COPY package.json package-lock.json ./
RUN npm ci --legacy-peer-deps

# Copy source and build the standalone server.
COPY . .
ARG NEXT_PUBLIC_API_URL=""
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
RUN npm run build

# ── Stage 2: Production runner ────────────────────────────────────────────────
FROM node:20-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000

# Copy standalone server (includes only the required node_modules).
COPY --from=builder /app/.next/standalone ./
# Copy static assets and public files (not bundled in standalone).
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD wget --no-verbose --tries=1 --spider http://localhost:3000/ || exit 1

CMD ["node", "server.js"]
