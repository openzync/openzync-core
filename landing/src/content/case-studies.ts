// ═══════════════════════════════════════════════════════════════════════════════
// OpenZep Landing — Use Cases / Case Studies Data
// ═══════════════════════════════════════════════════════════════════════════════

export interface CaseStudy {
  title: string;
  slug: string;
  excerpt: string;
  category: string;
  image?: string;
}

export const caseStudies: CaseStudy[] = [
  {
    title: "Modernising Customer Support with Persistent Agent Memory",
    slug: "customer-support",
    excerpt:
      "A large e-commerce platform reduced ticket resolution time by 60% by giving their support agents persistent memory of every customer interaction across sessions.",
    category: "Customer Support",
  },
  {
    title: "Research Assistant That Remembers Your Workflow",
    slug: "research-assistant",
    excerpt:
      "A pharmaceutical research team deployed OpenZep-powered agents that maintain context across weeks-long literature review sessions with citation graphs.",
    category: "Research",
  },
  {
    title: "Automated Code Review with Contextual Awareness",
    slug: "code-review",
    excerpt:
      "An engineering team built a code review agent that remembers project conventions, past PR feedback, and architectural decisions across the entire repository history.",
    category: "Engineering",
  },
];

export const allCaseStudiesHref = "/use-cases";
