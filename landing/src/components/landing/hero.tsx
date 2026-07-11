import Link from "next/link";
import { ArrowRight, GitFork, GitBranch, Shield, Puzzle } from "lucide-react";
import { Button } from "@openzync/design-system";
import { siteConfig } from "@/content/site-config";

interface ValueBadge {
  icon: typeof GitBranch;
  title: string;
}

const valueBadges: ValueBadge[] = [
  { icon: GitBranch, title: "Multi-Graph Backends" },
  { icon: Shield, title: "Enterprise Security" },
  { icon: Puzzle, title: "100% Open Source" },
];

/**
 * Hero section — two-column split:
 * Left: headline + subtext + CTAs + compact value badges
 * Right: terminal mockup showing OpenZync API code
 */
export function Hero() {
  return (
    <section className="relative min-h-screen flex items-center justify-center overflow-hidden pt-24 lg:pt-16">
      {/* Decorative grid pattern */}
      <div
        className="absolute inset-0 opacity-[0.03]"
        style={{
          backgroundImage:
            "linear-gradient(rgba(255,255,255,0.1) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,0.1) 1px, transparent 1px)",
          backgroundSize: "64px 64px",
        }}
      />

      {/* Content */}
      <div className="relative z-10 mx-auto max-w-7xl px-6 w-full">
        <div className="flex flex-col lg:flex-row lg:items-center lg:gap-16">
          {/* ── Left column ────────────────────────────────────────────── */}
          <div className="flex-1 lg:max-w-[55%] text-center lg:text-left">
            {/* Headline */}
            <h1 className="text-4xl sm:text-5xl md:text-6xl lg:text-7xl font-extrabold tracking-tight leading-tight">
              Persistent Memory
              <br />
              <span className="text-brand-300">for AI Agents</span>
            </h1>

            {/* Subtext */}
            <p className="mt-6 text-base sm:text-lg md:text-xl text-surface-400 max-w-2xl lg:mx-0 mx-auto leading-relaxed">
              OpenZync gives your agents durable, graph-based memory across 10+ backends
              and 5+ LLM providers. Build agents that remember, reason, and scale.
            </p>

            {/* Dual CTAs */}
            <div className="mt-8 flex flex-col sm:flex-row items-center lg:justify-start justify-center gap-4">
              <Link href={`${siteConfig.appUrl}/signup`}>
                <Button variant="primary" size="lg" icon={<ArrowRight size={18} />}>
                  Get Started Free
                </Button>
              </Link>
              <Link href={siteConfig.links.github}>
                <Button variant="secondary" size="lg" icon={<GitFork size={18} />}>
                  View on GitHub
                </Button>
              </Link>
            </div>

            {/* Compact value badges */}
            <div className="mt-10 flex flex-wrap items-center justify-center lg:justify-start gap-2.5">
              {valueBadges.map((badge) => {
                const Icon = badge.icon;
                return (
                  <div
                    key={badge.title}
                    className="inline-flex items-center gap-2 rounded-lg border border-surface-700/50 bg-surface-900 px-3 py-1.5"
                  >
                    <Icon size={14} className="text-brand-300 shrink-0" />
                    <span className="text-sm font-medium text-text-primary">
                      {badge.title}
                    </span>
                  </div>
                );
              })}
            </div>
          </div>

          {/* ── Right column — terminal (desktop only) ────────────────── */}
          <div className="hidden lg:block flex-1 lg:max-w-[45%]">
            <div className="animate-fade-in-up" style={{ animationDelay: "300ms" }}>
              <TerminalMockup />
            </div>
          </div>
        </div>
      </div>

    </section>
  );
}

/**
 * Terminal window mockup showing OpenZync API code.
 * Pure CSS — no gradients, no glass, no glow.
 */
function TerminalMockup() {
  return (
    <div className="rounded-xl border border-surface-700/50 bg-surface-900 shadow-2xl overflow-hidden">
      {/* Title bar */}
      <div className="flex items-center gap-1.5 px-4 py-2.5 bg-surface-800/80 border-b border-surface-700/50">
        <div className="flex gap-1.5">
          <span className="h-2.5 w-2.5 rounded-full bg-red-500/70" />
          <span className="h-2.5 w-2.5 rounded-full bg-yellow-500/70" />
          <span className="h-2.5 w-2.5 rounded-full bg-green-500/70" />
        </div>
        <span className="ml-3 text-[11px] text-surface-500 font-mono">memory_demo.py</span>
      </div>

      {/* Code content */}
      <div className="p-5 font-mono text-[13px] leading-relaxed text-surface-300">
        <div>
          <span className="text-accent-400">from</span> openzync{" "}
          <span className="text-accent-400">import</span>{" "}
          <span className="text-brand-300">AgentMemory</span>
        </div>

        <div className="mt-1.5">
          <span className="text-surface-600"># Initialize persistent memory</span>
        </div>
        <div className="mt-1">
          memory = <span className="text-brand-300">AgentMemory</span>(
        </div>
        <div className="ml-4">
          backend=<span className="text-accent-400">"neo4j"</span>,
        </div>
        <div className="ml-4">
          llm=<span className="text-accent-400">"claude-sonnet"</span>,
        </div>
        <div className="ml-4">
          api_key=<span className="text-surface-600">os.getenv</span>(
          <span className="text-accent-400">"OPENZYNC_KEY"</span>)
        </div>
        <div>)</div>

        <div className="mt-2">
          <span className="text-surface-600"># Store conversational memory</span>
        </div>
        <div className="mt-1">
          <span className="text-accent-400">await</span> memory.store(
        </div>
        <div className="ml-4">
          session_id=<span className="text-accent-400">"session_abc"</span>,
        </div>
        <div className="ml-4">
          data={"\u007B "}<span className="text-accent-400">"role"</span>: <span className="text-accent-400">"user"</span>,
        </div>
        <div className="ml-8">
          <span className="text-accent-400">"content"</span>: <span className="text-accent-400">"..."</span>,
        </div>
        <div className="ml-8">{"}"}</div>
        <div>)</div>

        <div className="mt-2">
          <span className="text-surface-600"># Retrieve relevant context</span>
        </div>
        <div className="mt-1">
          results = <span className="text-accent-400">await</span> memory.search(
        </div>
        <div className="ml-4">
          query=<span className="text-accent-400">"previous project context"</span>,
        </div>
        <div className="ml-4">
          limit=<span className="text-brand-300">5</span>
        </div>
        <div>)</div>

        <div className="mt-2 text-brand-300/50">
          <span className="text-surface-600"># </span>✓ Retrieved 3 relevant memories in 42ms
        </div>
      </div>
    </div>
  );
}
