import Link from "next/link";
import { statsData } from "@/content/stats";

/**
 * Stats section — "OpenZep by Numbers" (Plone's "Plone by Numbers" pattern).
 * Displays large stat items with label, value, and sub-description.
 */
export function StatsSection() {
  return (
    <section className="py-20 md:py-28 border-t border-surface-800">
      <div className="mx-auto max-w-7xl px-6">
        <h2 className="text-3xl md:text-4xl font-bold tracking-tight text-center mb-14">
          OpenZep by Numbers
        </h2>

        <div className="grid grid-cols-2 md:grid-cols-4 gap-8 max-w-4xl mx-auto">
          {statsData.map((stat) => (
            <div key={stat.label} className="text-center">
              <div className="text-3xl md:text-4xl font-bold text-brand-300">
                {stat.value}
              </div>
              <div className="text-sm font-medium text-text-primary mt-1">
                {stat.label}
              </div>
              <div className="text-xs text-surface-500 mt-1">
                {stat.description}
                {stat.link && (
                  <Link
                    href={stat.link.href}
                    className="block text-brand-300 hover:text-brand-200 transition-colors mt-0.5"
                  >
                    {stat.link.label}
                  </Link>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
