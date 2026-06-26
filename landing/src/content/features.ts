// ═══════════════════════════════════════════════════════════════════════════════
// OpenZep Landing — Features Configuration
// ═══════════════════════════════════════════════════════════════════════════════

export interface Feature {
  title: string;
  description: string;
  icon: string;
  category: "graph" | "memory" | "llm" | "tools" | "observability";
}

export const features: Feature[] = [
  {
    title: "Multi-Graph Backends",
    description:
      "Seamlessly switch between Neo4j, FalkorDB, Memgraph, and more. Your agent memory isn't locked into any single vendor.",
    icon: "GitBranch",
    category: "graph",
  },
  {
    title: "Graph-Based Memory",
    description:
      "Store agent interactions as a knowledge graph — entities, relationships, and facts that your agents can traverse and reason over.",
    icon: "Network",
    category: "memory",
  },
  {
    title: "Multi-LLM Support",
    description:
      "Works with OpenAI, Anthropic, Google, and local models. Route different tasks to different providers based on cost, latency, or capability.",
    icon: "Brain",
    category: "llm",
  },
  {
    title: "Human-in-the-Loop",
    description:
      "Define approval gates for any action with external side effects — email, payments, API writes. Every escalation is auditable and resumable.",
    icon: "Shield",
    category: "tools",
  },
  {
    title: "Tool Plugin System",
    description:
      "Extend agent capabilities with a typed plugin interface. Every tool is idempotent, retry-safe, and emits structured observability data.",
    icon: "Puzzle",
    category: "tools",
  },
  {
    title: "Deterministic Workflows",
    description:
      "Stage-gated DAGs over open-ended ReAct loops for production workflows. Crash-safe, resumable, and auditable at every transition.",
    icon: "Workflow",
    category: "tools",
  },
  {
    title: "Observability Built-In",
    description:
      "Every tool execution, LLM call, and workflow transition emits structured logs with latency, token counts, and cost estimates out of the box.",
    icon: "BarChart3",
    category: "observability",
  },
  {
    title: "Prompt Versioning",
    description:
      "Version-controlled prompt templates with eval suites. Every prompt change goes through the same PR review as code changes.",
    icon: "FileCode",
    category: "llm",
  },
  {
    title: "Credit Billing",
    description:
      "Usage-based billing per agent session with configurable credit limits. Built-in rate limiting and quota enforcement.",
    icon: "CreditCard",
    category: "observability",
  },
];

export const categories: {
  key: Feature["category"];
  label: string;
  description: string;
}[] = [
  {
    key: "graph",
    label: "Graph Backends",
    description: "The most flexible graph storage layer in the industry",
  },
  {
    key: "memory",
    label: "Agent Memory",
    description: "Persistent, structured memory that agents can reason over",
  },
  {
    key: "llm",
    label: "LLM Providers",
    description: "Works with every major LLM provider",
  },
  {
    key: "tools",
    label: "Tool Ecosystem",
    description: "Extensible plugin system for agent capabilities",
  },
  {
    key: "observability",
    label: "Observability",
    description: "Built for production debugging and cost control",
  },
];
