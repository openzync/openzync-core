// ═══════════════════════════════════════════════════════════════════════════════
// OpenZep Landing — FAQ Data
// ═══════════════════════════════════════════════════════════════════════════════

export interface FaqItem {
  question: string;
  answer: string;
}

export const faqItems: FaqItem[] = [
  {
    question: "What is OpenZep?",
    answer:
      "OpenZep is an open-source, persistent memory infrastructure for AI agents. It stores agent interactions as a knowledge graph — entities, relationships, and facts — that agents can read from, write to, and reason over across sessions.",
  },
  {
    question: "How is OpenZep different from a regular database?",
    answer:
      "Unlike a generic database, OpenZep is purpose-built for agent memory. It provides graph-based storage optimized for agentic access patterns, built-in support for multiple graph backends, LLM-integrated retrieval, human-in-the-loop workflows, and observability — all behind a single, clean API.",
  },
  {
    question: "What graph backends does OpenZep support?",
    answer:
      "OpenZep supports Neo4j, FalkorDB, Memgraph, Amazon Neptune, and more. The backend is swappable via configuration — your agent memory isn't locked into any single vendor.",
  },
  {
    question: "Which LLM providers can I use with OpenZep?",
    answer:
      "OpenZep works with OpenAI, Anthropic (Claude), Google (Gemini), and local models through Ollama or vLLM. You can route different tasks to different providers based on cost, latency, or capability requirements.",
  },
  {
    question: "Is OpenZep open source?",
    answer:
      "Yes, OpenZep is 100% open source under the Apache 2.0 license. It's backed by the OpenZep Foundation. You can self-host, modify, and extend it freely.",
  },
  {
    question: "Can I use OpenZep for free?",
    answer:
      "OpenZep is free and open source for self-hosted deployments. We also offer a managed cloud service with a free tier that includes 100,000 memory operations per month — no credit card required.",
  },
  {
    question: "How does human-in-the-loop work?",
    answer:
      "You define approval gates for any action with external side effects — email, payments, API writes. When a gate is triggered, the workflow pauses and awaits human approval. Every escalation is logged with full context for audit trails.",
  },
  {
    question: "What languages/frameworks does OpenZep support?",
    answer:
      "OpenZep provides a Python SDK as a first-class citizen, plus a REST API that works with any language. TypeScript/JavaScript SDK is in development. The plugin system is language-agnostic via the REST API.",
  },
];
