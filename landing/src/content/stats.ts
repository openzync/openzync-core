// ═══════════════════════════════════════════════════════════════════════════════
// OpenZep Landing — Statistics Data
// ═══════════════════════════════════════════════════════════════════════════════

export interface StatItem {
  value: string;
  label: string;
  description: string;
  link?: { label: string; href: string };
}

export const statsData: StatItem[] = [
  {
    value: "10+",
    label: "Graph Backends",
    description: "Neo4j, FalkorDB, Memgraph, and more supported",
  },
  {
    value: "5",
    label: "LLM Providers",
    description: "OpenAI, Anthropic, Google, and local models",
  },
  {
    value: "1024",
    label: "Contributors",
    description: "On GitHub",
    link: { label: "In GitHub", href: "https://github.com/openzep/openzep/graphs/contributors" },
  },
  {
    value: "1.0",
    label: "Latest Version",
    description: "Install",
    link: { label: "Install", href: "https://github.com/openzep/openzep/releases" },
  },
];
