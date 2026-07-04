// ═══════════════════════════════════════════════════════════════════════════════
// OpenZync Landing — Site Configuration
// ═══════════════════════════════════════════════════════════════════════════════

export const siteConfig = {
  name: "OpenZync",
  tagline: "Persistent Agent Memory Infrastructure",
  description:
    "OpenZync provides persistent, graph-based memory for AI agents. Store, retrieve, and reason over agent interactions at scale with support for 10+ graph backends and 5+ LLM providers.",
  url: "https://openzync.tech",
  appUrl: process.env.NEXT_PUBLIC_APP_URL ?? "https://app.openzync.tech",
  ogImage: "/images/og-default.svg",
  links: {
    github: "https://github.com/rohnsha0/openzync",
    twitter: "https://twitter.com/openzync",
    docs: "https://docs.openzync.tech",
    discord: "https://discord.gg/openzync",
  },
};

// ─── Navigation (Mega-menu) ─────────────────────────────────────────────────

export interface NavItem {
  label: string;
  href?: string;
  children?: { label: string; href: string; description?: string }[];
  highlights?: { label: string; href: string }[];
}

export const navItems: NavItem[] = [
  {
    label: "Why OpenZync",
    children: [
      { label: "What is OpenZync?", href: "/about", description: "Overview and mission" },
      { label: "Features", href: "/features", description: "Everything OpenZync offers" },
      { label: "Use Cases", href: "/use-cases", description: "Real-world applications" },
      { label: "FAQ", href: "/faq", description: "Common questions" },
    ],
  },
  {
    label: "Get Started",
    children: [
      { label: "Quickstart", href: "https://docs.openzync.tech/quickstart", description: "5-minute setup" },
      { label: "Documentation", href: "https://docs.openzync.tech", description: "Full reference" },
      { label: "GitHub", href: "https://github.com/rohnsha0/openzync", description: "Source code" },
      { label: "API Reference", href: "https://docs.openzync.tech/api", description: "REST & SDK API docs" },
    ],
    highlights: [
      { label: "Try the Demo", href: "https://demo.openzync.tech" },
      { label: "View on GitHub", href: "https://github.com/rohnsha0/openzync" },
    ],
  },
  {
    label: "Community",
    children: [
      { label: "GitHub Discussions", href: "https://github.com/rohnsha0/openzync/discussions", description: "Ask and answer questions" },
      { label: "Discord", href: "https://discord.gg/openzync", description: "Real-time chat" },
      { label: "Contributing Guide", href: "https://docs.openzync.tech/contributing", description: "How to contribute" },
      { label: "Support", href: "https://docs.openzync.tech/support", description: "Get help" },
    ],
    highlights: [
      { label: "Join Discord", href: "https://discord.gg/openzync" },
    ],
  },
  {
    label: "News & Updates",
    children: [
      { label: "Blog", href: "/blog", description: "Engineering, releases, and community" },
      { label: "Changelog", href: "/changelog", description: "Version release notes" },
      { label: "Releases", href: "https://github.com/rohnsha0/openzync/releases", description: "GitHub releases" },
    ],
    highlights: [
      { label: "Latest Release v1.0.0", href: "https://github.com/rohnsha0/openzync/releases/tag/v1.0.0" },
    ],
  },
  {
    label: "Foundation",
    children: [
      { label: "About Us", href: "/about", description: "Our mission and team" },
      { label: "Contact", href: "mailto:hello@openzync.tech", description: "Get in touch" },
      { label: "Sponsorship", href: "https://github.com/sponsors/openzync", description: "Support the project" },
      { label: "Press Kit", href: "/press-kit", description: "Brand assets" },
    ],
  },
];

// ─── Breadcrumb path labels ─────────────────────────────────────────────────

export const breadcrumbLabels: Record<string, string> = {
  "/": "Home",
  "/features": "Features",
  "/about": "About",
  "/changelog": "Changelog",
  "/blog": "Blog",
  "/use-cases": "Use Cases",
  "/faq": "FAQ",
  "/events": "Events",
  "/press-kit": "Press Kit",
  "/privacy": "Privacy Policy",
  "/terms": "Terms of Service",
};

// ─── Footer links ───────────────────────────────────────────────────────────

export const footerLinks = {
  product: [
    { label: "Features", href: "/features" },
    { label: "Changelog", href: "/changelog" },
    { label: "Documentation", href: "https://docs.openzync.tech" },
    { label: "Roadmap", href: "https://docs.openzync.tech/roadmap" },
    { label: "API Reference", href: "https://docs.openzync.tech/api" },
  ],
  community: [
    { label: "GitHub", href: "https://github.com/rohnsha0/openzync" },
    { label: "Discussions", href: "https://github.com/rohnsha0/openzync/discussions" },
    { label: "Discord", href: "https://discord.gg/openzync" },
    { label: "Contributing", href: "https://docs.openzync.tech/contributing" },
    { label: "Support", href: "https://docs.openzync.tech/support" },
  ],
  updates: [
    { label: "Blog", href: "/blog" },
    { label: "Changelog", href: "/changelog" },
    { label: "Releases", href: "https://github.com/rohnsha0/openzync/releases" },
  ],
  company: [
    { label: "About", href: "/about" },
    { label: "Contact", href: "mailto:hello@openzync.tech" },
    { label: "Press Kit", href: "/press-kit" },
    { label: "Sponsorship", href: "https://github.com/sponsors/openzync" },
  ],
  legal: [
    { label: "Privacy Policy", href: "/privacy" },
    { label: "Terms of Service", href: "/terms" },
  ],
};

export const footerSocialLinks = [
  { label: "GitHub", href: "https://github.com/rohnsha0/openzync", icon: "github" },
  { label: "Twitter", href: "https://twitter.com/openzync", icon: "twitter" },
  { label: "LinkedIn", href: "https://linkedin.com/company/openzync", icon: "linkedin" },
  { label: "YouTube", href: "https://youtube.com/@openzync", icon: "youtube" },
];
