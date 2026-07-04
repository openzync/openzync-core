import { cn } from "@/lib/utils";
import { features, categories, type Feature } from "@/content/features";
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

// ─── Icon map ─────────────────────────────────────────────────────────────────

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

// ─── Feature card ─────────────────────────────────────────────────────────────

function FeatureCard({ feature, index }: { feature: Feature; index: number }) {
  const Icon = iconMap[feature.icon] ?? Puzzle;
  return (
    <div
      className={cn(
        "card-interactive p-6",
        "animate-fade-in-up",
      )}
      style={{ animationDelay: `${(index % 6) * 80}ms` }}
    >
      <div className="flex h-11 w-11 items-center justify-center rounded-lg bg-brand-500/10 mb-4">
        <Icon size={22} className="text-brand-300" />
      </div>
      <h3 className="text-base font-semibold text-text-primary mb-2">
        {feature.title}
      </h3>
      <p className="text-sm text-surface-400 leading-relaxed">
        {feature.description}
      </p>
    </div>
  );
}

// ─── Features grid ────────────────────────────────────────────────────────────

interface FeaturesGridProps {
  className?: string;
  /** If true, shows all features. If a category key, filters to that category. */
  filter?: Feature["category"] | "all";
}

export function FeaturesGrid({ className, filter = "all" }: FeaturesGridProps) {
  const displayed =
    filter === "all"
      ? features
      : features.filter((f) => f.category === filter);

  return (
    <div
      className={cn(
        "grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4",
        className,
      )}
    >
      {displayed.map((feature, i) => (
        <FeatureCard key={feature.title} feature={feature} index={i} />
      ))}
    </div>
  );
}

// ─── Features section (full page) ─────────────────────────────────────────────

export function FeaturesSection() {
  return (
    <section className="py-20 md:py-28">
      <div className="mx-auto max-w-7xl px-6">
        {/* Header */}
        <div className="text-center mb-16">
          <h2 className="text-3xl md:text-4xl font-bold tracking-tight mb-4">
            Everything you need for{" "}
            <span className="text-brand-300">
              Agent Memory
            </span>
          </h2>
          <p className="text-surface-400 max-w-2xl mx-auto">
            From graph backends to observability — OpenZync provides the full
            stack for production-grade agent memory infrastructure.
          </p>
        </div>

        {/* Category sections */}
        {categories.map((cat) => {
          const catFeatures = features.filter((f) => f.category === cat.key);
          return (
            <div key={cat.key} className="mb-16 last:mb-0">
              <div className="mb-6">
                <h3 className="text-xl font-semibold text-text-primary mb-1">
                  {cat.label}
                </h3>
                <p className="text-sm text-surface-400">{cat.description}</p>
              </div>
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                {catFeatures.map((feature, i) => (
                  <FeatureCard key={feature.title} feature={feature} index={i} />
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
