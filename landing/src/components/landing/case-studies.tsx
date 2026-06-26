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
              {/* Image placeholder */}
              <div className="h-40 bg-gradient-to-br from-brand-500/10 to-accent-500/5 flex items-center justify-center">
                <span className="text-3xl font-bold text-brand-500/30">{cs.category[0]}</span>
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
