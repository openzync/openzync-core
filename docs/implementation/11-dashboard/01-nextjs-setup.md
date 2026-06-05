# Next.js Dashboard Setup — Implementation Guide

> **Domain:** Admin Dashboard
> **SRS Phase:** Phase 4 — Dashboard & SDKs (Week 10-12)
> **Requirements:** DASH-01, DASH-08
> **Doc Dependencies:** [02-auth-tenancy/02-jwt-auth.md](../02-auth-tenancy/02-jwt-auth.md), [02-auth-tenancy/03-tenant-isolation.md](../02-auth-tenancy/03-tenant-isolation.md)

---

## 1. Overview

The OpenZep admin dashboard is a Next.js 14 application using the App Router with a `src/` directory structure. It provides a web UI for platform administrators to manage organisations (tenants), users, knowledge graphs, and usage analytics.

### 1.1 Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Next.js 14 App Router** | Team standard. File-based routing, React Server Components, middleware for auth. |
| **JWT auth with HttpOnly cookies** | Prevents XSS token theft. 15-min access token + 7-day refresh token with rotation. |
| **API calls via Next.js API routes** | Protects backend API credentials. Browser never sees the REST API key. |
| **OpenAPI-typescript client** | Auto-generated, type-safe API client from the OpenAPI 3.1 spec. No manual endpoint definitions. |
| **shadcn/ui + Tailwind** | Consistent, accessible component library. Easy to theme. No prop-drilling hell. |
| **Static export where possible** | Simpler deployment. Dashboard pages that need SSR (e.g., auth) get it; admin pages are static. |

### 1.2 Architecture

```
Browser                          Next.js App                          FastAPI Backend
   │                                 │                                     │
   │  GET /dashboard/login           │                                     │
   │────────────────────────────────►│                                     │
   │◄─── Login page ───────────────│                                     │
   │                                 │                                     │
   │  POST /api/auth/login          │                                     │
   │  {email, password}             │  POST /v1/admin/auth/login          │
   │────────────────────────────────►│───────────────────────────────────►│
   │                                 │◄─── {access_token, refresh_token}──│
   │◄─── Set HttpOnly cookie ──────│                                     │
   │                                 │                                     │
   │  GET /dashboard/orgs           │                                     │
   │  (cookie sent automatically)   │  GET /v1/admin/organizations        │
   │────────────────────────────────►│───────────────────────────────────►│
   │                                 │  (with REST API key internally)    │
   │◄─── Org list page ────────────│◄─── Organizations list ────────────│
```

---

## 2. Project Setup

### 2.1 Initialisation

```bash
npx create-next-app@latest apps/dashboard --typescript --tailwind --eslint --app --src-dir --import-alias "@/*"
cd apps/dashboard
```

### 2.2 Dependencies

```bash
# Core
npm install next@14 react react-dom

# UI (shadcn/ui)
npx shadcn-ui@latest init
npx shadcn-ui@latest add button card input table dialog dropdown-menu
npx shadcn-ui@latest add toast sheet tabs badge separator

# Auth
npm install jose         # JWT verification (edge-compatible)
npm install bcryptjs     # Password hashing

# API client
npm install openapi-typescript openapi-fetch

# Icons
npm install lucide-react

# Graph viz
npm install cytoscape  # Used in graph explorer page

# Charts (for analytics)
npm install recharts

# Forms
npm install react-hook-form @hookform/resolvers zod
```

### 2.3 Directory Structure

```
apps/dashboard/
├── src/
│   ├── app/
│   │   ├── layout.tsx              # Root layout with providers
│   │   ├── page.tsx                # Redirects to /dashboard
│   │   ├── login/
│   │   │   └── page.tsx            # Login page (no layout wrapper)
│   │   ├── dashboard/
│   │   │   ├── layout.tsx          # Sidebar + header layout
│   │   │   ├── page.tsx            # Dashboard overview / redirect
│   │   │   ├── orgs/
│   │   │   │   ├── page.tsx        # Organisation list
│   │   │   │   ├── new/
│   │   │   │   │   └── page.tsx    # Create organisation
│   │   │   │   └── [id]/
│   │   │   │       └── page.tsx    # Organisation detail
│   │   │   ├── users/
│   │   │   │   └── [userId]/
│   │   │   │       └── page.tsx    # User detail
│   │   │   ├── graph/
│   │   │   │   └── [orgId]/[userId]/
│   │   │   │       └── page.tsx    # Graph explorer
│   │   │   └── analytics/
│   │   │       └── page.tsx        # Analytics dashboard
│   │   └── api/
│   │       ├── auth/
│   │       │   ├── login/route.ts
│   │       │   ├── logout/route.ts
│   │       │   └── refresh/route.ts
│   │       └── proxy/
│   │           └── [...path]/route.ts  # Proxy to backend API
│   ├── components/
│   │   ├── ui/                     # shadcn/ui components
│   │   ├── layout/
│   │   │   ├── sidebar.tsx
│   │   │   ├── header.tsx
│   │   │   └── breadcrumbs.tsx
│   │   ├── orgs/
│   │   │   ├── org-list.tsx
│   │   │   ├── org-create-form.tsx
│   │   │   └── org-quota-editor.tsx
│   │   ├── graph/
│   │   │   └── graph-canvas.tsx
│   │   └── analytics/
│   │       ├── api-rps-chart.tsx
│   │       ├── error-rate-chart.tsx
│   │       ├── latency-chart.tsx
│   │       └── token-usage-chart.tsx
│   ├── lib/
│   │   ├── api.ts                  # API client (openapi-fetch instance)
│   │   ├── auth.ts                 # Auth utilities
│   │   └── utils.ts                # Shared utilities (cn(), etc.)
│   ├── hooks/
│   │   ├── use-auth.ts
│   │   ├── use-orgs.ts
│   │   └── use-users.ts
│   └── middleware.ts               # Auth middleware (JWT check + redirect)
├── public/
├── .env.local                      # NEXT_PUBLIC_API_URL
├── next.config.js
├── tailwind.config.ts
└── tsconfig.json
```

---

## 3. Authentication

### 3.1 Auth Flow

1. User visits `/dashboard/*` → middleware checks JWT cookie
2. If no valid token → redirect to `/login`
3. User submits credentials → `POST /api/auth/login`
4. Server validates against backend, returns access + refresh tokens
5. Access token stored in HttpOnly cookie (15-min expiry)
6. Refresh token stored in separate HttpOnly cookie (7-day, rotated on use)
7. Middleware silently refreshes expired access tokens using refresh token

### 3.2 Login API Route

```typescript
// src/app/api/auth/login/route.ts
import { NextRequest, NextResponse } from "next/server";
import { SignJWT } from "jose";

const JWT_SECRET = new TextEncoder().encode(process.env.JWT_SECRET!);
const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8000";

export async function POST(request: NextRequest) {
  const { email, password } = await request.json();

  // Validate credentials against the backend
  const backendResponse = await fetch(
    `${BACKEND_URL}/v1/admin/auth/login`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    }
  );

  if (!backendResponse.ok) {
    return NextResponse.json(
      { error: "Invalid credentials" },
      { status: 401 }
    );
  }

  const { access_token, refresh_token, user } = await backendResponse.json();

  // Create access token cookie (15min)
  const accessCookie = `access_token=${access_token}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=900`;

  // Create refresh token cookie (7 days)
  const refreshCookie = `refresh_token=${refresh_token}; HttpOnly; Secure; SameSite=Strict; Path=/api/auth; Max-Age=604800`;

  const response = NextResponse.json({ user });
  response.headers.set("Set-Cookie", [accessCookie, refreshCookie].join(", "));

  return response;
}
```

### 3.3 Middleware

```typescript
// src/middleware.ts
import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { jwtVerify } from "jose";

const JWT_SECRET = new TextEncoder().encode(process.env.JWT_SECRET!);

// Pages that don't require authentication
const publicPaths = ["/login", "/api/auth/login"];

export async function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // Allow public paths and API auth routes
  if (publicPaths.some((p) => pathname.startsWith(p))) {
    return NextResponse.next();
  }

  // Check for access token
  const accessToken = request.cookies.get("access_token")?.value;

  if (!accessToken) {
    // No token at all — check for refresh token
    const refreshToken = request.cookies.get("refresh_token")?.value;
    if (refreshToken) {
      // Attempt silent refresh by calling the refresh endpoint
      const refreshResponse = await fetch(
        `${request.nextUrl.origin}/api/auth/refresh`,
        {
          headers: { Cookie: `refresh_token=${refreshToken}` },
        }
      );

      if (refreshResponse.ok) {
        // Refresh succeeded — forward the new cookies and retry
        const newResponse = NextResponse.next();
        const setCookie = refreshResponse.headers.get("Set-Cookie");
        if (setCookie) {
          newResponse.headers.set("Set-Cookie", setCookie);
        }
        return newResponse;
      }
    }

    // No valid tokens — redirect to login
    const loginUrl = new URL("/login", request.url);
    loginUrl.searchParams.set("redirect", pathname);
    return NextResponse.redirect(loginUrl);
  }

  // Verify access token hasn't expired
  try {
    await jwtVerify(accessToken, JWT_SECRET);
    return NextResponse.next();
  } catch {
    // Token expired — redirect to login
    const loginUrl = new URL("/login", request.url);
    loginUrl.searchParams.set("redirect", pathname);
    return NextResponse.redirect(loginUrl);
  }
}

// Only run middleware on dashboard routes
export const config = {
  matcher: ["/dashboard/:path*"],
};
```

### 3.4 Frontend Auth Hook

```typescript
// src/hooks/use-auth.ts
"use client";

import { createContext, useContext, useState, useEffect, ReactNode } from "react";

interface User {
  id: string;
  email: string;
  name: string;
  role: "admin" | "super_admin";
}

interface AuthContext {
  user: User | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContext | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // Check if user is authenticated on mount
    fetch("/api/auth/me")
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => setUser(data))
      .finally(() => setLoading(false));
  }, []);

  const login = async (email: string, password: string) => {
    const res = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.error || "Login failed");
    }

    const { user } = await res.json();
    setUser(user);
  };

  const logout = async () => {
    await fetch("/api/auth/logout", { method: "POST" });
    setUser(null);
  };

  return (
    <AuthContext.Provider value={{ user, loading, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used within AuthProvider");
  return context;
}
```

---

## 4. API Client

### 4.1 Generate from OpenAPI Spec

```bash
# Generate TypeScript types and client from the backend's OpenAPI spec
npx openapi-typescript http://localhost:8000/openapi.json -o src/lib/api/schema.ts

# Or from a local file
npx openapi-typescript apps/api/openapi.json -o src/lib/api/schema.ts
```

### 4.2 API Client Instance

```typescript
// src/lib/api.ts
import createClient from "openapi-fetch";
import type { paths } from "./api/schema";

// API client that proxies through Next.js API routes.
// The browser never calls the backend directly — all requests
// go through /api/proxy/..., which adds the REST API key server-side.
export const apiClient = createClient<paths>({
  baseUrl: "/api/proxy/v1",
});

// Helper for auth-required pages
export async function fetchWithAuth(url: string, options?: RequestInit) {
  return fetch(url, {
    ...options,
    credentials: "include", // Send cookies
  });
}
```

### 4.3 Proxy Route (Protects Backend Credentials)

```typescript
// src/app/api/proxy/[...path]/route.ts
import { NextRequest, NextResponse } from "next/server";

const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8000";
const BACKEND_API_KEY = process.env.BACKEND_API_KEY!; // Server-side only

export async function GET(
  request: NextRequest,
  { params }: { params: { path: string[] } }
) {
  const path = params.path.join("/");
  const searchParams = request.nextUrl.searchParams.toString();
  const url = `${BACKEND_URL}/v1/${path}${searchParams ? `?${searchParams}` : ""}`;

  const response = await fetch(url, {
    headers: {
      Authorization: `Bearer ${BACKEND_API_KEY}`,
      "Content-Type": "application/json",
    },
  });

  const data = await response.json();
  return NextResponse.json(data, { status: response.status });
}

// Same handler for POST, PUT, PATCH, DELETE
export const POST = handler;
export const PUT = handler;
export const PATCH = handler;
export const DELETE = handler;

async function handler(request: NextRequest, { params }: { params: { path: string[] } }) {
  const path = params.path.join("/");
  const url = `${BACKEND_URL}/v1/${path}`;

  const body = request.body ? await request.json() : undefined;

  const response = await fetch(url, {
    method: request.method,
    headers: {
      Authorization: `Bearer ${BACKEND_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: body ? JSON.stringify(body) : undefined,
  });

  const data = response.headers.get("content-type")?.includes("application/json")
    ? await response.json()
    : await response.text();

  return NextResponse.json(data, { status: response.status });
}
```

---

## 5. Pages and Routing

### 5.1 Route Map

| Path | Page | Auth | Description |
|------|------|------|-------------|
| `/` | Root page | No | Redirects to `/dashboard` |
| `/login` | Login page | No | Email + password login |
| `/dashboard` | Overview | Yes | Dashboard home (redirect to orgs) |
| `/dashboard/orgs` | Org list | Yes | List all organisations |
| `/dashboard/orgs/new` | Create org | Yes | Create new organisation |
| `/dashboard/orgs/[id]` | Org detail | Yes | View/edit org, manage API keys, quotas |
| `/dashboard/orgs/[orgId]/users/[userId]/graph` | Graph explorer | Yes | Interactive graph visualisation |
| `/dashboard/analytics` | Analytics | Yes | Usage charts and metrics |

### 5.2 Dashboard Layout

```typescript
// src/app/dashboard/layout.tsx
import { Sidebar } from "@/components/layout/sidebar";
import { Header } from "@/components/layout/header";

export default function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="flex h-screen">
      <Sidebar />
      <div className="flex-1 flex flex-col">
        <Header />
        <main className="flex-1 overflow-y-auto p-6">
          {children}
        </main>
      </div>
    </div>
  );
}
```

### 5.3 Sidebar Component

```typescript
// src/components/layout/sidebar.tsx
"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Building2,
  BarChart3,
  Users,
  Network,
  Settings,
} from "lucide-react";
import { cn } from "@/lib/utils";

const navItems = [
  { href: "/dashboard/orgs", label: "Organisations", icon: Building2 },
  { href: "/dashboard/users", label: "Users", icon: Users },
  { href: "/dashboard/graph", label: "Graph Explorer", icon: Network },
  { href: "/dashboard/analytics", label: "Analytics", icon: BarChart3 },
  { href: "/dashboard/settings", label: "Settings", icon: Settings },
];

export function Sidebar() {
  const pathname = usePathname();

  return (
    <aside className="w-64 border-r bg-sidebar flex flex-col">
      <div className="p-4 border-b">
        <Link href="/dashboard" className="text-lg font-bold">
          OpenZep
        </Link>
        <p className="text-xs text-muted-foreground">Admin Dashboard</p>
      </div>
      <nav className="flex-1 p-2 space-y-1">
        {navItems.map((item) => {
          const Icon = item.icon;
          const isActive = pathname.startsWith(item.href);
          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                "flex items-center gap-3 px-3 py-2 rounded-md text-sm transition-colors",
                isActive
                  ? "bg-primary text-primary-foreground"
                  : "hover:bg-muted"
              )}
            >
              <Icon className="h-4 w-4" />
              {item.label}
            </Link>
          );
        })}
      </nav>
    </aside>
  );
}
```

---

## 6. UI Framework Configuration

### 6.1 shadcn/ui Init

```bash
npx shadcn-ui@latest init
```

When prompted:
- Style: Default (New York)
- Base color: Neutral
- CSS variables: Yes
- React Server Components: Yes

### 6.2 Tailwind Config

```typescript
// tailwind.config.ts
import type { Config } from "tailwindcss";

const config: Config = {
  darkMode: ["class"],
  content: [
    "./src/**/*.{ts,tsx}",
  ],
  theme: {
    container: {
      center: true,
      padding: "2rem",
      screens: {
        "2xl": "1400px",
      },
    },
    extend: {
      colors: {
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        destructive: {
          DEFAULT: "hsl(var(--destructive))",
          foreground: "hsl(var(--destructive-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        sidebar: {
          DEFAULT: "hsl(var(--sidebar-background))",
          foreground: "hsl(var(--sidebar-foreground))",
        },
      },
    },
  },
  plugins: [require("tailwindcss-animate")],
};

export default config;
```

---

## 7. Build and Deployment

### 7.1 Static Export (Simpler Deployment)

If the dashboard is deployed separately from the backend, use static export:

```javascript
// next.config.js
/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "export",  // Static HTML export
  images: {
    unoptimized: true,  // Required for static export
  },
  // Disable server-side features
  trailingSlash: true,
};

module.exports = nextConfig;
```

Build command:
```bash
npm run build
# Output in out/
```

Serve with any static file server (nginx, S3, Cloudflare Pages):
```bash
npx serve out -p 3000
```

### 7.2 Full SSR (For Auth and Dynamic Pages)

For features requiring SSR (auth, dynamic data), use the default Next.js server:

```javascript
// next.config.js — no output: "export"
/** @type {import('next').NextConfig} */
const nextConfig = {
  // Standard server mode
};

module.exports = nextConfig;
```

### 7.3 Docker Multi-Stage Build

```dockerfile
# apps/dashboard/Dockerfile
# Stage 1: Build
FROM node:20-alpine AS builder
WORKDIR /app

# Install dependencies
COPY package.json package-lock.json ./
RUN npm ci --frozen-lockfile

# Copy source
COPY . .

# Build
ARG NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
RUN npm run build

# Stage 2: Serve (for SSR mode)
FROM node:20-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

RUN addgroup --system --gid 1001 nodejs
RUN adduser --system --uid 1001 nextjs

COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 3000

ENV PORT=3000
ENV HOSTNAME="0.0.0.0"

CMD ["node", "server.js"]

# Alternative: Serve static export with nginx
FROM nginx:alpine AS static
COPY --from=builder /app/out /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
```

### 7.4 Nginx Config for Static Export

```nginx
# apps/dashboard/nginx.conf
server {
    listen 80;
    server_name _;
    root /usr/share/nginx/html;

    # SPA-style routing — all paths serve index.html
    location / {
        try_files $uri $uri.html $uri/ /index.html;
    }

    # Cache static assets
    location /_next/static {
        expires 1y;
        add_header Cache-Control "public, immutable";
    }
}
```

### 7.5 Docker Compose Integration

```yaml
# docker-compose.yml (in root)
services:
  dashboard:
    build:
      context: ./apps/dashboard
      args:
        NEXT_PUBLIC_API_URL: "http://localhost:8000"
    ports:
      - "3000:3000"
    environment:
      BACKEND_URL: "http://api:8000"
      BACKEND_API_KEY: "${MEMGRAPH_API_KEY}"
      JWT_SECRET: "${JWT_SECRET}"
    depends_on:
      - api
```

---

## 8. Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `NEXT_PUBLIC_API_URL` | Yes | Public URL of the backend (e.g., `http://localhost:8000`) |
| `BACKEND_URL` | Yes | Internal URL of the backend (e.g., `http://api:8000`) |
| `BACKEND_API_KEY` | Yes | Server-side API key for proxying requests |
| `JWT_SECRET` | Yes | Secret for signing/verifying dashboard JWT tokens |
| `NEXT_PUBLIC_APP_URL` | No | Public dashboard URL (for CORS and redirects) |

---

## 9. Testing

### 9.1 Unit Tests

```typescript
// __tests__/middleware.test.ts
import { describe, it, expect } from "vitest";

describe("Auth middleware", () => {
  it("redirects to login when no token is present", async () => {
    // Mock request without cookies
    // Assert redirect to /login
  });

  it("allows access with valid token", async () => {
    // Mock request with valid access_token cookie
    // Assert NextResponse.next() is called
  });

  it("attempts refresh with expired access token", async () => {
    // Mock request with expired access token + valid refresh token
    // Assert refresh endpoint is called
  });
});
```

### 9.2 Integration Tests

```typescript
// __tests__/login.test.ts
import { test, expect } from "@playwright/test";

test("login flow", async ({ page }) => {
  await page.goto("/login");
  await page.fill('input[name="email"]', "admin@example.com");
  await page.fill('input[name="password"]', "password123");
  await page.click('button[type="submit"]');
  await expect(page).toHaveURL("/dashboard/orgs");
});

test("redirects to login when unauthenticated", async ({ page }) => {
  await page.goto("/dashboard/orgs");
  await expect(page).toHaveURL(/\/login/);
});
```

---

## 10. Open Questions

| # | Question | Decision |
|---|----------|----------|
| Q1 | Should we use static export or SSR? | Use SSR for now — auth requires server-side cookie handling. Static export is available for simpler deployments. |
| Q2 | Should we support dark mode? | Yes — shadcn/ui has built-in dark mode support. Add toggle in header. |
| Q3 | Should the dashboard support i18n? | No — defer until Phase 5. English only for MVP. |
| Q4 | PWA / offline support? | No — dashboard requires backend connectivity to be useful. |

---

*Corresponding SRS requirements: DASH-01, DASH-08. Next: [02-tenant-management.md](02-tenant-management.md) for organisation management UI.*
