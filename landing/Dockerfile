# ═══════════════════════════════════════════════════════════════════════════════
# OpenZep Landing — Production Dockerfile
# ═══════════════════════════════════════════════════════════════════════════════

# ── Builder stage ──────────────────────────────────────────────────────────────
FROM node:20-alpine AS builder
WORKDIR /app

# Install OpenSSL (required by Next.js standalone)
RUN apk add --no-cache openssl

# Copy workspace root
COPY package.json package-lock.json ./

# Copy landing app and design-system package
COPY landing/package.json landing/
COPY packages/design-system/ packages/design-system/

# Install all workspace dependencies
RUN npm ci

# Copy source code
COPY landing/ landing/
COPY packages/design-system/ packages/design-system/

# Build the design system first
RUN npm run -w @openzep/design-system build

# Build the landing app
RUN npm run -w landing build

# ── Runtime stage ──────────────────────────────────────────────────────────────
FROM node:20-alpine AS runtime
WORKDIR /app

RUN addgroup --system --gid 1001 nodejs
RUN adduser --system --uid 1001 nextjs

# Copy standalone output from builder
COPY --from=builder /app/landing/.next/standalone ./
COPY --from=builder /app/landing/.next/static ./landing/.next/static
COPY --from=builder /app/landing/public ./landing/public

# Set correct ownership
RUN chown -R nextjs:nodejs /app

USER nextjs

ENV NODE_ENV=production
ENV PORT=3000

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD wget --no-verbose --tries=1 --spider http://localhost:3000/ || exit 1

CMD ["node", "landing/server.js"]
