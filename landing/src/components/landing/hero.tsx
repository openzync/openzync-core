import Link from "next/link";
import { ArrowRight, GitFork, GitBranch, Shield, Puzzle, ChevronDown } from "lucide-react";
import { Button } from "@openzep/design-system";
import { siteConfig } from "@/content/site-config";

interface ValueCard {
  icon: typeof GitBranch;
  title: string;
  description: string;
  ctaLabel: string;
  ctaHref: string;
}

const valueCards: ValueCard[] = [
  {
    icon: GitBranch,
    title: "Multi-Graph Backends",
    description:
      "Seamlessly switch between Neo4j, FalkorDB, Memgraph, and more. No vendor lock-in.",
    ctaLabel: "Explore Backends",
    ctaHref: "https://docs.openzep.com/backends",
  },
  {
    icon: Shield,
    title: "Enterprise Security",
    description:
      "Human-in-the-loop gates, audit trails, and circuit breakers. Production safety by design.",
    ctaLabel: "Security Model",
    ctaHref: "https://docs.openzep.com/security",
  },
  {
    icon: Puzzle,
    title: "Open & Free",
    description:
      "100% open source under Apache 2.0. Self-host or use our cloud — your data belongs to you.",
    ctaLabel: "View on GitHub",
    ctaHref: siteConfig.links.github,
  },
];

/**
 * Hero section following Plone.org's pattern:
 * big headline + sub-headline + 3 value-prop cards + dual CTAs below the cards.
 */
export function Hero() {
  return (
    <section className="relative min-h-screen flex items-center justify-center overflow-hidden">
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
      <div className="relative z-10 mx-auto max-w-6xl px-6 text-center">
        {/* Headline */}
        <h1 className="text-4xl sm:text-5xl md:text-6xl lg:text-7xl font-extrabold tracking-tight leading-tight">
          Persistent Memory
          <br />
          <span className="text-brand-300">
            for AI Agents
          </span>
        </h1>

        {/* Subtext */}
        <p className="mt-6 text-base sm:text-lg md:text-xl text-surface-400 max-w-2xl mx-auto leading-relaxed">
          OpenZep gives your agents durable, graph-based memory across 10+ backends
          and 5+ LLM providers. Build agents that remember, reason, and scale.
        </p>

        {/* ─── 3 Value Cards (Plone pattern) ─────────────────────────────── */}
        <div className="mt-14 grid grid-cols-1 md:grid-cols-3 gap-5 text-left">
          {valueCards.map((card, idx) => {
            const Icon = card.icon;
            const isFirst = idx === 0;
            return (
              <div
                key={card.title}
                className={`card-interactive p-6 flex flex-col ${
                  isFirst
                    ? "border-brand-500/40 bg-brand-500/[0.04]"
                    : ""
                }`}
              >
                <div className="flex h-11 w-11 items-center justify-center rounded-lg bg-brand-500/10 mb-4">
                  <Icon size={22} className="text-brand-300" />
                </div>
                <h3 className="text-base font-semibold text-text-primary mb-2">
                  {card.title}
                </h3>
                <p className="text-sm text-surface-400 leading-relaxed flex-1 mb-4">
                  {card.description}
                </p>
                <Link
                  href={card.ctaHref}
                  className="text-sm font-medium text-brand-300 hover:text-brand-200 transition-colors inline-flex items-center gap-1 group"
                >
                  {card.ctaLabel}
                  <ArrowRight size={14} className="card-cta-arrow" />
                </Link>
              </div>
            );
          })}
        </div>

        {/* ─── Dual CTAs (below cards, same as before) ───────────────────── */}
        <div className="mt-10 flex flex-col sm:flex-row items-center justify-center gap-4">
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
      </div>

      {/* Scroll indicator */}
      <div className="absolute bottom-0 left-0 right-0 h-16 flex items-end justify-center pb-4 pointer-events-none">
        <div className="flex flex-col items-center gap-1.5 text-surface-600">
          <span className="text-[10px] uppercase tracking-widest font-medium">Scroll</span>
          <ChevronDown size={16} className="animate-bounce-down" />
        </div>
      </div>
    </section>
  );
}
