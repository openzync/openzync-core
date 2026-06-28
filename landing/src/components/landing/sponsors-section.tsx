import Link from "next/link";
import { ArrowRight, GitFork } from "lucide-react";

/**
 * Sponsor section — Plone's "Our Sponsors" pattern.
 * Logo grid with sponsor logos + "See all" CTA.
 */
export function SponsorsSection() {
  return (
    <section className="py-20 md:py-28 border-t border-surface-800">
      <div className="mx-auto max-w-7xl px-6 text-center">
        <h2 className="text-3xl md:text-4xl font-bold tracking-tight mb-4">
          Trusted By
        </h2>
        <p className="text-surface-400 max-w-xl mx-auto mb-12">
          OpenZep is built with support from our community and sponsoring organizations.
        </p>

        {/* Logo grid */}
        <div className="flex flex-wrap items-center justify-center gap-6 max-w-3xl mx-auto mb-10">
          {["OpenAI", "Neo4j", "Anthropic", "FalkorDB", "Memgraph", "Google"].map((name) => (
            <div
              key={name}
              className="flex items-center justify-center h-14 px-8 rounded-xl border border-surface-700/60 bg-surface-900/80 shadow-sm"
            >
              <span className="text-sm font-bold text-surface-400 tracking-wider uppercase">
                {name}
              </span>
            </div>
          ))}
          <Link
            href="https://github.com/sponsors/openzep"
            className="flex items-center justify-center h-14 px-8 rounded-xl border border-dashed border-surface-700 bg-surface-900/50 hover:bg-surface-800/80 transition-colors group"
          >
            <span className="text-sm font-medium text-surface-500 flex items-center gap-1.5 group-hover:text-surface-300 transition-colors">
              <GitFork size={16} />
              Put your logo here
            </span>
          </Link>
        </div>

        <Link
          href="https://github.com/sponsors/openzep"
          className="inline-flex items-center gap-1.5 text-sm font-medium text-brand-300 hover:text-brand-200 transition-colors"
        >
          Become a sponsor — every contribution shapes our future!
          <ArrowRight size={14} />
        </Link>
      </div>
    </section>
  );
}
