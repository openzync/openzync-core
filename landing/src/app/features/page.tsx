import type { Metadata } from "next";
import { ArrowRight, GitFork } from "lucide-react";
import { Breadcrumbs, buildBreadcrumbSegments } from "@/components/landing/breadcrumbs";
import { features, categories } from "@/content/features";
import { FadeIn } from "@/components/landing/fade-in";
import { Button } from "@openzync/design-system";
import { siteConfig } from "@/content/site-config";
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

export const metadata: Metadata = {
  title: "Features",
  description:
    "Explore OpenZync's feature set: multi-graph backends, agent memory, multi-LLM support, tool plugins, observability, and more.",
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
 * Features page:
 * - Intro header with breadcrumbs
 * - Sections grouped by category (icon + heading + feature list)
 * - CTA at bottom
 */
export default function FeaturesPage() {
  const segments = buildBreadcrumbSegments("/features");

  return (
    <>
      {/* Header */}
      <section className="pt-36 pb-12 md:pb-16">
        <div className="mx-auto max-w-4xl px-6">
          <Breadcrumbs segments={segments} />

          <h1 className="text-4xl md:text-5xl font-extrabold tracking-tight mb-4">
            Features
          </h1>
          <p className="text-surface-400 text-lg max-w-2xl">
            OpenZync provides the full stack for production-grade agent memory
            infrastructure — from graph backends to observability, built for
            scale, security, and multi-provider flexibility.
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
                OpenZync is {cat.label}
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

      {/* Try / Download CTA */}
      <section className="py-16 border-t border-surface-800">
        <div className="mx-auto max-w-3xl px-6 text-center">
          <h2 className="text-2xl md:text-3xl font-bold tracking-tight mb-4">
            Try OpenZync Yourself
          </h2>
          <p className="text-surface-400 mb-8 max-w-xl mx-auto">
            You can try OpenZync using our demo site or download it to run locally.
          </p>
          <div className="flex flex-col sm:flex-row items-center justify-center gap-4">
            <a href="https://demo.openzync.tech" target="_blank" rel="noopener noreferrer">
              <Button variant="primary" size="lg" icon={<ArrowRight size={18} />}>
                Try the Demo
              </Button>
            </a>
            <a href="https://github.com/rohnsha0/openzync/releases" target="_blank" rel="noopener noreferrer">
              <Button variant="secondary" size="lg" icon={<GitFork size={18} />}>
                Download
              </Button>
            </a>
          </div>
        </div>
      </section>

      <CtaSection />
    </>
  );
}
