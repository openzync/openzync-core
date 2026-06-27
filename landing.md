## Full Plan — OpenZep Landing Page

---

### Phase 0: Monorepo Scaffolding

**Files to create:**
```
/package.json                          ← npm workspaces root
/packages/design-system/package.json   ← @openzep/design-system
/packages/design-system/tsconfig.json
/packages/design-system/src/globals.css
/packages/design-system/src/cn.ts
/packages/design-system/src/components/button.tsx
/packages/design-system/src/components/badge.tsx
/packages/design-system/src/components/spinner.tsx
/packages/design-system/src/index.ts
```

**Root `package.json`:**
```json
{
  "private": true,
  "workspaces": ["landing", "packages/design-system"]
}
```

**`@openzep/design-system` scope:**
- `globals.css` — exact theme tokens from the dashboard (brand 50-900, surface 50-950, semantic colors, fonts, animation keyframes, `.card-base`, `.input-base`, reduced-motion), re-exported as-is
- `cn.ts` — `clsx` + `tailwind-merge` (identical to dashboard's version)
- `button.tsx` — same 4-variant/3-size/loading/icon system
- `badge.tsx` — same CVA-based variants
- `spinner.tsx` — same SVG spinner
- Build: TypeScript compiled to `dist/`, CSS published alongside

---

### Phase 1: Landing App Scaffolding

**Files to create:**
```
/landing/package.json
/landing/next.config.ts              ← output: "standalone", MDX plugin
/landing/tsconfig.json               ← strict, @/ → ./src/*
/landing/postcss.config.mjs          ← @tailwindcss/postcss
/landing/Dockerfile                  ← multi-stage, same pattern as frontend/
/landing/.env.example
```

**Key `package.json` dependencies:**
```json
{
  "dependencies": {
    "@openzep/design-system": "*",
    "next": "16.2.9",
    "react": "19.2.4",
    "react-dom": "19.2.4",
    "next-themes": "^0.4.6",
    "lucide-react": "^1.18.0",
    "clsx": "^2.1.1",
    "tailwind-merge": "^3.6.0",
    "@next/mdx": "^16",
    "@mdx-js/loader": "^3",
    "@mdx-js/react": "^3",
    "tailwindcss": "^4.3.1",
    "@tailwindcss/postcss": "^4.3.1",
    "tailwindcss-animate": "^1.0.7"
  }
}
```

---

### Phase 2: Design System Package

| File | Content |
|---|---|
| `src/globals.css` | Full `@theme` block copied from dashboard (brand, surface, semantic, fonts) + `@keyframes fadeIn`, `slideUp`, `pulse-dot` + `.card-base`, `.input-base`, `.animate-*` utilities + `prefers-reduced-motion` |
| `src/cn.ts` | `export function cn(...inputs: ClassValue[]): string { return twMerge(clsx(inputs)); }` |
| `src/components/button.tsx` | Same `variant`/`size`/`loading`/`icon` props, same styling |
| `src/components/badge.tsx` | Same CVA variants, same `StatusBadge`/`ActorTypeBadge` helpers |
| `src/components/spinner.tsx` | Same SVG `animate-spin` spinner |
| `src/index.ts` | Re-exports: `cn`, `Button`, `Badge`, `StatusBadge`, `Spinner` |

---

### Phase 3: Landing Components

#### 3a — Layout Shell (`landing/src/app/`)

| File | Purpose |
|---|---|
| `globals.css` | `@import "@openzep/design-system/globals.css"` + landing-specific utilities |
| `layout.tsx` | **Server component.** Loads Inter + JetBrains Mono via `next/font/google`. Wraps children in `ThemeProvider` (dark default). Renders `<Navbar />` + `{children}` + `<Footer />`. Metadata: title "OpenZep — Agent Memory Infrastructure", description, OG tags |
| `page.tsx` | Imports and composes `<Hero />` + `<FeaturesPreview />` + `<StatsBar />` + `<CtaSection />` |

#### 3b — Landing Components (`landing/src/components/landing/`)

**`navbar.tsx`**
- Fixed top, transparent → glass effect on scroll (`backdrop-blur-md`)
- Left: Logo mark (brand blue "O" + "OpenZep")
- Center: Nav links — Features, Changelog, About
- Right: "Sign In" (ghost) + "Get Started" (primary CTA)
- Mobile: hamburger → slide-down overlay menu with same links
- Active link highlighting based on current path
- Design language: `bg-surface-950/80 backdrop-blur-md border-b border-surface-800`

**`hero.tsx`**
- Full-viewport height (min-h-screen)
- Centered content: gradient background (`bg-gradient-to-b from-brand-500/5 via-surface-950 to-surface-950`)
- Decorative: subtle grid pattern or radial gradient overlay
- Headline: `text-5xl md:text-7xl font-extrabold tracking-tight` — "Persistent Memory for AI Agents"
- Subtext: `text-lg md:text-xl text-surface-400 max-w-2xl` — explaining the product value prop
- Dual CTA: "Get Started" (primary, large) + "Read Docs" (secondary, large)
- Social proof: logo cloud of supported integrations (LangChain, LlamaIndex, etc.) or stat bar
- Fade-in animation on mount

**`features-grid.tsx`**
- Section header: "Everything you need for Agent Memory" + subtitle
- 3-column grid on desktop, 2 on tablet, 1 on mobile
- Each card: icon in a `bg-brand-500/10` circle → title → description
- Cards use `.card-interactive` class from design tokens
- Data driven from `/landing/src/content/features.ts`

**`cta-section.tsx`**
- Full-width section with subtle brand gradient background
- Headline: "Ready to give your agents persistent memory?"
- Subtext: "Start building in minutes"
- CTA button (primary, large)
- Centered, generous padding

**`footer.tsx`**
- Multi-column layout: Logo + description | Product | Company | Legal | Social
- `border-t border-surface-800` at top
- Copyright line at bottom: "© 2026 OpenZep. All rights reserved."
- Link styling: `text-surface-400 hover:text-text-primary transition-colors`

#### 3c — Page: Features (`landing/src/app/features/page.tsx`)

- Server component
- Section header with intro text
- Full `FeaturesGrid` component
- Could have multiple sections (Graph Backends, LLM Providers, Memory Types, etc.)
- Bottom CTA section

#### 3d — Page: About (`landing/src/app/about/page.tsx`)

- Server component or MDX
- Company mission, team values
- Could include a small team section (if applicable)
- Styled as prose content

#### 3e — Page: Changelog

**`landing/src/app/changelog/page.tsx`** (list view):
- Fetch all MDX files from `content/changelog/`
- Render as a chronological list: date, version badge, title, excerpt
- Each entry links to `changelog/[slug]`

**`landing/src/app/changelog/[slug]/page.tsx`** (detail):
- Dynamic route, renders MDX content
- Back link to /changelog
- MDX content styled with Tailwind prose classes

**`landing/content/changelog/`** — MDX files:
```mdx
---
title: "OpenZep v1.0.0 — Launch"
date: "2026-06-01"
version: "1.0.0"
---

## What's new

- Persistent memory storage with 10+ graph backends
- Multi-LLM support
- Human-in-the-loop workflows
```

---

### Phase 4: Content Configuration

**`landing/src/content/features.ts`** — typed data file:
```typescript
export interface Feature {
  title: string;
  description: string;
  icon: string; // Lucide icon name
  category: "memory" | "llm" | "graph" | "tools";
}

export const features: Feature[] = [
  {
    title: "Multi-Graph Backends",
    description: "Support for Neo4j, FalkorDB, Memgraph, and more...",
    icon: "GitBranch",
    category: "graph",
  },
  // ...
];
```

---

### Phase 5: Responsive & Animation Design

**Responsive breakpoints** (carried from dashboard):
- `sm: 640px` — tablet adjustments
- `md: 768px` — multi-column grids activate
- `lg: 1024px` — full desktop layout
- `max-w-7xl mx-auto` for content centering

**Responsive behavior per section:**
| Section | Mobile | Tablet | Desktop |
|---|---|---|---|
| Navbar | Hamburger + overlay | Hamburger + overlay | Full horizontal links |
| Hero | Stacked, smaller text | Stacked, larger text | Side-by-side or centered |
| Features grid | 1 col | 2 cols | 3 cols |
| Footer | 2 columns | 3 columns | 4 columns |

**Animations** (carried from dashboard):
- `animate-fade-in` on page content (0.2s ease-out)
- `animate-slide-up` on dropdowns/menus (0.25s ease-out)
- CSS transitions: `transition-all duration-150` on buttons, `duration-200` on cards
- `prefers-reduced-motion: reduce` block — same as dashboard

**New landing-only animations (subtle):**
- Hero fade-in on load (`opacity-0 animate-fade-in` with `animation-delay`)
- Staggered card entrance on features grid (`animate-slide-up` with increasing delay per card)
- Navbar glass effect transition on scroll

---

### Phase 6: Route Structure

```
openzep.com/                    → Hero page (landing/src/app/page.tsx)
openzep.com/features            → Features page
openzep.com/about               → About page
openzep.com/changelog           → Changelog list
openzep.com/changelog/[slug]    → Changelog entry (MDX)
```

All pages are **server components** (no "use client") except where interactivity requires it (Navbar hamburger toggle, theme toggle, mobile menu). This ensures SSR for SEO.

---

### Phase 7: SEO & Metadata

All pages include:
```typescript
export const metadata: Metadata = {
  title: "Page Title | OpenZep",
  description: "Page-specific description",
  openGraph: {
    title: "...",
    description: "...",
    images: [{ url: "/images/og-default.png" }],
  },
};
```

Global layout metadata includes canonical URL (`openzep.com`), Twitter card tags, and structured JSON-LD for the organization.

---

### Phase 8: Deployment

**Landing Dockerfile** (same pattern as `frontend/`):
```dockerfile
FROM node:20-alpine AS builder
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:20-alpine AS runtime
WORKDIR /app
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/public ./public
ENV NODE_ENV=production
EXPOSE 3000
CMD ["node", "server.js"]
```

- Deployed to `openzep.com` (subdomain)
- CDN caching: ISR with `revalidate` on changelog pages, static generation for landing pages
- Environment variables:  `NEXT_PUBLIC_APP_URL=https://app.openzep.com` (for "Sign In" links back to dashboard)
- Health check: `GET /health` → 200

---

### Phase 9: Implementation Order

| Step | Description | Dependencies |
|---|---|---|
| 1 | Root `package.json` + npm workspaces setup | None |
| 2 | `packages/design-system/` — tokens, cn(), Button, Badge, Spinner | None |
| 3 | `landing/` — Next.js scaffolding, config files, Dockerfile | Step 2 |
| 4 | Navbar + Footer + root layout + ThemeProvider | Step 3 |
| 5 | Hero section | Step 4 |
| 6 | Features content config + FeaturesGrid component | Step 4 |
| 7 | `/features` page | Step 6 |
| 8 | `/about` page | Step 4 |
| 9 | MDX setup + Changelog list + detail pages | Step 3 |
| 10 | CTA section + polish (animations, responsive, SEO metadata) | Steps 4-9 |
| 11 | Final review: responsive audit, reduced-motion, Lighthouse | Step 10 |

---

### Design Language Consistency Checklist

Every component will be checked against:
- [ ] Uses `cn()` for class merging
- [ ] Uses brand palette tokens (`var(--color-brand-500)`, `text-brand-300`, `bg-surface-900`)
- [ ] Follows existing spacing rhythm (`p-6`, `gap-4`, `space-y-6`)
- [ ] Typography: Inter sans body, JetBrains Mono for code, consistent heading sizes
- [ ] Icons from Lucide, same sizes (14/16/18/20/22)
- [ ] Hover states use `transition-all duration-150`
- [ ] Cards use `border-surface-800` borders with hover elevation
- [ ] Dark background `#0D1117` (surface-950)
- [ ] Reduced motion respected
- [ ] Server components by default, "use client" only where needed

---

**Want me to proceed with implementation in this order?**
