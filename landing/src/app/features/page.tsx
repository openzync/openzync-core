import type { Metadata } from "next";
import { Breadcrumbs, buildBreadcrumbSegments } from "@/components/landing/breadcrumbs";
import { features, categories } from "@/content/features";
import {
  GitBranch,
  Network,
  Brain,
  Shield,
  Puzzle,
  Workflow,
  BarChart3,
  FileCode,
  CreditCard,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { CtaSection } from "@/components/landing/cta-section";
import { FadeIn } from "@/components/landing/fade-in";

export const metadata: Metadata = {
  title: "Features",
  description:
    "Explore OpenZep's feature set: multi-graph backends, agent memory, multi-LLM support, tool plugins, observability, and more.",
};

const iconMap: Record<string, LucideIcon> = {
  GitBranch,
  Network,
  Brain,
  Shield,
  Puzzle,
  Workflow,
  BarChart3,
  FileCode,
  CreditCard,
};

/**
 * Features page in Plone style:
 * - Quote block + intro
 * - Sections grouped by category (icon + heading + feature list)
 * - "Download or Try" CTA at bottom
 */
export default function FeaturesPage() {
  const segments = buildBreadcrumbSegments("/features");

  return (
    <>
      {/* Header */}
      <section className="pt-36 pb-12 md:pb-16">
        <div className="mx-auto max-w-4xl px-6">
          <Breadcrumbs segments={segments} />

          {/* Quote block (Plone pattern) */}
          <blockquote className="border-l-3 border-brand-500 pl-5 mb-8">
            <p className="text-lg text-surface-400 italic leading-relaxed">
              &ldquo;Plone has only one feature — there is no thing it cannot do&rdquo;
            </p>
            <footer className="text-sm text-surface-500 mt-1">
              — <cite className="not-italic">Mikko Ohtamaa</cite>
            </footer>
          </blockquote>

          <h1 className="text-4xl md:text-5xl font-extrabold tracking-tight mb-4">
            Features
          </h1>
          <p className="text-surface-400 text-lg max-w-2xl">
            This site is built with OpenZep and demonstrates the many capabilities
            of agent memory infrastructure. Everything you need to build
            production-grade agent systems.
          </p>
        </div>
      </section>

      {/* Feature sections grouped by category (Plone style) */}
      {categories.map((cat) => {
        const catFeatures = features.filter((f) => f.category === cat.key);
        return (
          <section key={cat.key} className="py-16 border-t border-surface-800">
            <div className="mx-auto max-w-4xl px-6">
              <h2 className="text-2xl md:text-3xl font-bold tracking-tight mb-2">
                OpenZep is {cat.label}
              </h2>
              <p className="text-surface-400 mb-10">{cat.description}</p>

              <div className="space-y-8">
                {catFeatures.map((feature) => {
                  const Icon = iconMap[feature.icon] ?? Puzzle;
                  return (
                    <div
                      key={feature.title}
                      className="flex gap-5 items-start"
                    >
                      <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg bg-brand-500/10 mt-0.5">
                        <Icon size={22} className="text-brand-300" />
                      </div>
                      <div>
                        <h3 className="text-lg font-semibold text-text-primary mb-1">
                          {feature.title}
                        </h3>
                        <p className="text-surface-400 leading-relaxed text-sm">
                          {feature.description}
                        </p>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          </section>
        );
      })}

      {/* Download/Try CTA (Plone pattern) */}
      <section className="py-16 border-t border-surface-800">
        <div className="mx-auto max-w-3xl px-6 text-center">
          <h2 className="text-2xl md:text-3xl font-bold tracking-tight mb-4">
            Try OpenZep Yourself
          </h2>
          <p className="text-surface-400 mb-6">
            You can try OpenZep using our demo site or download it to run locally.
          </p>
          <ul className="space-y-2 mb-6">
            <li className="text-sm text-surface-300">
              <span className="text-brand-300 mr-2">■</span>
              OpenZep Demo:{' '}
              <a href="https://demo.openzep.com" className="text-brand-300 hover:text-brand-200 transition-colors">
                https://demo.openzep.com
              </a>
            </li>
            <li className="text-sm text-surface-300">
              <span className="text-brand-300 mr-2">■</span>
              Download:{' '}
              <a href="https://github.com/openzep/openzep/releases" className="text-brand-300 hover:text-brand-200 transition-colors">
                https://github.com/openzep/openzep/releases
              </a>
            </li>
          </ul>
        </div>
      </section>

      <CtaSection />
    </>
  );
}
