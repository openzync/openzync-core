// ═══════════════════════════════════════════════════════════════════════════════
// OpenZep Landing — Site Configuration
// ═══════════════════════════════════════════════════════════════════════════════

export const siteConfig = {
  name: "OpenZep",
  tagline: "Persistent Agent Memory Infrastructure",
  description:
    "OpenZep provides persistent, graph-based memory for AI agents. Store, retrieve, and reason over agent interactions at scale with support for 10+ graph backends and 5+ LLM providers.",
  url: "https://openzep.com",
  appUrl: process.env.NEXT_PUBLIC_APP_URL ?? "https://app.openzep.com",
  ogImage: "/images/og-default.svg",
  links: {
    github: "https://github.com/openzep",
    twitter: "https://twitter.com/openzep",
    docs: "https://docs.openzep.com",
    discord: "https://discord.gg/openzep",
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
    label: "Why OpenZep",
    children: [
      { label: "What is OpenZep?", href: "/about", description: "Overview and mission" },
      { label: "Features", href: "/features", description: "Everything OpenZep offers" },
      { label: "Use Cases", href: "/use-cases", description: "Real-world applications" },
      { label: "FAQ", href: "/faq", description: "Common questions" },
    ],
  },
  {
    label: "Get Started",
    children: [
      { label: "Quickstart", href: "https://docs.openzep.com/quickstart", description: "5-minute setup" },
      { label: "Documentation", href: "https://docs.openzep.com", description: "Full reference" },
      { label: "GitHub", href: "https://github.com/openzep/openzep", description: "Source code" },
      { label: "API Reference", href: "https://docs.openzep.com/api", description: "REST & SDK API docs" },
    ],
    highlights: [
      { label: "Try the Demo", href: "https://demo.openzep.com" },
      { label: "View on GitHub", href: "https://github.com/openzep" },
    ],
  },
  {
    label: "Community",
    children: [
      { label: "GitHub Discussions", href: "https://github.com/openzep/openzep/discussions", description: "Ask and answer questions" },
      { label: "Discord", href: "https://discord.gg/openzep", description: "Real-time chat" },
      { label: "Contributing Guide", href: "https://docs.openzep.com/contributing", description: "How to contribute" },
      { label: "Support", href: "https://docs.openzep.com/support", description: "Get help" },
    ],
    highlights: [
      { label: "Join Discord", href: "https://discord.gg/openzep" },
    ],
  },
  {
    label: "News & Updates",
    children: [
      { label: "Blog", href: "/blog", description: "Engineering, releases, and community" },
      { label: "Changelog", href: "/changelog", description: "Version release notes" },
      { label: "Releases", href: "https://github.com/openzep/openzep/releases", description: "GitHub releases" },
    ],
    highlights: [
      { label: "Latest Release v1.0.0", href: "https://github.com/openzep/openzep/releases/tag/v1.0.0" },
    ],
  },
  {
    label: "Foundation",
    children: [
      { label: "About Us", href: "/about", description: "Our mission and team" },
      { label: "Contact", href: "mailto:hello@openzep.com", description: "Get in touch" },
      { label: "Sponsorship", href: "https://github.com/sponsors/openzep", description: "Support the project" },
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
    { label: "Documentation", href: "https://docs.openzep.com" },
    { label: "Roadmap", href: "https://docs.openzep.com/roadmap" },
    { label: "API Reference", href: "https://docs.openzep.com/api" },
  ],
  community: [
    { label: "GitHub", href: "https://github.com/openzep/openzep" },
    { label: "Discussions", href: "https://github.com/openzep/openzep/discussions" },
    { label: "Discord", href: "https://discord.gg/openzep" },
    { label: "Contributing", href: "https://docs.openzep.com/contributing" },
    { label: "Support", href: "https://docs.openzep.com/support" },
  ],
  updates: [
    { label: "Blog", href: "/blog" },
    { label: "Changelog", href: "/changelog" },
    { label: "Releases", href: "https://github.com/openzep/openzep/releases" },
  ],
  company: [
    { label: "About", href: "/about" },
    { label: "Contact", href: "mailto:hello@openzep.com" },
    { label: "Press Kit", href: "/press-kit" },
    { label: "Sponsorship", href: "https://github.com/sponsors/openzep" },
  ],
  legal: [
    { label: "Privacy Policy", href: "/privacy" },
    { label: "Terms of Service", href: "/terms" },
  ],
};

export const footerSocialLinks = [
  { label: "GitHub", href: "https://github.com/openzep", icon: "github" },
  { label: "Twitter", href: "https://twitter.com/openzep", icon: "twitter" },
  { label: "LinkedIn", href: "https://linkedin.com/company/openzep", icon: "linkedin" },
  { label: "YouTube", href: "https://youtube.com/@openzep", icon: "youtube" },
];
