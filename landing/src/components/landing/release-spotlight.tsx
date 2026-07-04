import { ArrowDown } from "lucide-react";
import { Button } from "@openzync/design-system";
import { latestRelease } from "@/content/releases";

/**
 * Release spotlight section — latest version highlight
 * with version badge + heading + install terminal + description + CTA.
 */
export function ReleaseSpotlight() {
  return (
    <section className="py-20 md:py-28 border-t border-surface-800">
      <div className="mx-auto max-w-7xl px-6">
        <div className="max-w-3xl mx-auto text-center">
          {/* Version badge + heading */}
          <div className="mb-4">
            <span className="inline-flex items-center rounded-full border border-brand-500/30 bg-brand-500/10 px-3 py-1 text-xs font-semibold text-brand-300">
              v{latestRelease.version}
            </span>
          </div>
          <h2 className="text-3xl md:text-4xl font-bold tracking-tight text-text-primary mb-3">
            {latestRelease.title}
          </h2>

          {/* Inline install terminal */}
          <div className="inline-flex items-center gap-2 rounded-lg border border-surface-700/50 bg-surface-900 px-4 py-2 font-mono text-sm text-surface-300 mb-6">
            <span className="text-surface-600">$</span>
            <span className="text-accent-400">pip install</span>
            <span className="text-brand-300">openzync=={latestRelease.version}</span>
          </div>

          {/* Description */}
          <p className="text-surface-400 leading-relaxed max-w-xl mx-auto mb-8">
            {latestRelease.description}
          </p>

          {/* Download CTA */}
          <a
            href={latestRelease.downloadUrl}
            target="_blank"
            rel="noopener noreferrer"
          >
            <Button variant="primary" size="lg" icon={<ArrowDown size={18} />} className="active:scale-[0.97] transition-transform duration-100">
              Download the latest version
            </Button>
          </a>
        </div>
      </div>
    </section>
  );
}
