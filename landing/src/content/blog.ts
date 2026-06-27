// ═══════════════════════════════════════════════════════════════════════════════
// OpenZep Landing — Blog & News Data
// ═══════════════════════════════════════════════════════════════════════════════

export interface BlogPost {
  slug: string;
  title: string;
  date: string;
  excerpt: string;
  author: string;
  category: string;
  content: string;
}

export type BlogCategory = "engineering" | "community" | "releases" | "tutorials";

export const blogCategories: { key: BlogCategory; label: string }[] = [
  { key: "engineering", label: "Engineering" },
  { key: "community", label: "Community" },
  { key: "releases", label: "Releases" },
  { key: "tutorials", label: "Tutorials" },
];
