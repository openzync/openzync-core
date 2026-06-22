# ──────────────────────────────────────────────────────────────────────────────
# OpenZep Frontend — Production Docker image
# ──────────────────────────────────────────────────────────────────────────────
# Multi-stage build:
#   1. deps — installs npm dependencies (cached layer)
#   2. builder — builds the Next.js app with standalone output
#   3. runner — minimal image with only the production server + assets
#
# Usage:
#   docker build -t openzep-frontend:latest -f frontend/Dockerfile frontend/
#
# The frontend expects a reverse proxy (nginx) to route /v1/* requests
# to the API. NEXT_PUBLIC_API_URL defaults to "" (relative URLs), which
# works when the frontend and API share the same origin via the proxy.
# ──────────────────────────────────────────────────────────────────────────────

# ── Stage 1: Install dependencies ──────────────────────────────────────────────
FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --only=production

# ── Stage 2: Build ────────────────────────────────────────────────────────────
FROM node:20-alpine AS builder
WORKDIR /app

# Copy full dependency tree (dev deps needed for build)
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Build with standalone output.  NEXT_PUBLIC_API_URL is empty by default,
# meaning the frontend makes relative requests (e.g. /v1/sessions) that
# are proxied to the API via nginx.  Override with --build-arg if you
# serve frontend and API from different origins.
ARG NEXT_PUBLIC_API_URL=""
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL

RUN npm run build

# ── Stage 3: Production runner ────────────────────────────────────────────────
FROM node:20-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000

# Copy standalone server (includes only the needed node_modules)
COPY --from=builder /app/.next/standalone ./

# Copy static assets (not bundled in standalone)
COPY --from=builder /app/.next/static ./.next/static

# Copy public assets (favicon, images, etc.)
COPY --from=builder /app/public ./public

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD wget --no-verbose --tries=1 --spider http://localhost:3000/ || exit 1

CMD ["node", "server.js"]
