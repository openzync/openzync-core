import Link from "next/link";
import { ArrowRight } from "lucide-react";
import { caseStudies, allCaseStudiesHref } from "@/content/case-studies";

/**
 * Case studies / use cases section — Plone's "Where Plone Shines" pattern.
 * 3 cards with image placeholder + category + title + excerpt + "Read more".
 */
export function CaseStudies() {
  return (
    <section className="py-20 md:py-28 border-t border-surface-800">
      <div className="mx-auto max-w-7xl px-6">
        <h2 className="text-3xl md:text-4xl font-bold tracking-tight text-center mb-4">
          Where OpenZep Shines
        </h2>
        <p className="text-surface-400 text-center max-w-xl mx-auto mb-12">
          From customer support to research to engineering — teams use OpenZep
          to give their agents persistent memory.
        </p>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-6 max-w-5xl mx-auto">
          {caseStudies.map((cs) => (
            <Link
              key={cs.slug}
              href={`/use-cases#${cs.slug}`}
              className="card-interactive overflow-hidden flex flex-col"
            >
              {/* Card header — terminal-style code snippet */}
              <div className="h-40 bg-surface-900 flex items-center justify-center overflow-hidden border-b border-surface-800">
                <div className="relative w-full h-full opacity-50">
                  <div className="absolute inset-0 p-4 font-mono text-[10px] leading-relaxed text-brand-300/40 select-none">
                    <div className="text-brand-500/60"># graph_memory.py</div>
                    <div className="mt-1"><span className="text-accent-300/40">from</span> openzep <span className="text-accent-300/40">import</span> Agent</div>
                    <div className="mt-1"><span className="text-accent-300/40">async</span> <span className="text-accent-300/40">def</span> build_agent():</div>
                    <div className="ml-3">agent = <span className="text-brand-300/40">Agent</span>(</div>
                    <div className="ml-6">memory=<span className="text-brand-300/40">GraphMemory</span>(</div>
                    <div className="ml-9">backend=<span className="text-accent-300/40">"neo4j"</span>,</div>
                    <div className="ml-9">llm=<span className="text-accent-300/40">"claude"</span></div>
                    <div className="ml-6">)</div>
                    <div className="ml-3">return agent</div>
                  </div>
                </div>
              </div>
              <div className="p-5 flex-1 flex flex-col">
                <span className="inline-block text-[10px] uppercase tracking-widest font-semibold text-brand-300 mb-2">
                  {cs.category}
                </span>
                <h3 className="text-base font-semibold text-text-primary mb-2">
                  {cs.title}
                </h3>
                <p className="text-sm text-surface-400 leading-relaxed flex-1">
                  {cs.excerpt}
                </p>
                <span className="inline-flex items-center gap-1 text-sm font-medium text-brand-300 hover:text-brand-200 transition-colors mt-4">
                  Read more
                  <ArrowRight size={14} />
                </span>
              </div>
            </Link>
          ))}
        </div>

        {allCaseStudiesHref && (
          <div className="text-center mt-8">
            <Link
              href={allCaseStudiesHref}
              className="inline-flex items-center gap-1.5 text-sm font-medium text-brand-300 hover:text-brand-200 transition-colors"
            >
              See all use cases
              <ArrowRight size={14} />
            </Link>
          </div>
        )}
      </div>
    </section>
  );
}
