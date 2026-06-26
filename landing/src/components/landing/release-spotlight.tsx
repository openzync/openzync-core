import { ArrowDown, Share2, GitFork, Globe, Link as LinkIcon } from "lucide-react";
import { Button } from "@openzep/design-system";
import { latestRelease } from "@/content/releases";

const shareUrl = "https://openzep.com";
const shareText = "Check out OpenZep — Persistent Agent Memory Infrastructure";

/**
 * Release spotlight section — Plone's pattern:
 * version heading + social share icons + description + download CTA.
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

          {/* Social share icons (Plone pattern) */}
          <div className="flex items-center justify-center gap-2 mb-6 text-surface-500">
            <span className="text-xs flex items-center gap-1">
              <Share2 size={12} />
              Share:
            </span>
            <a
              href={`https://twitter.com/intent/tweet?url=${encodeURIComponent(shareUrl)}&text=${encodeURIComponent(shareText)}`}
              target="_blank"
              rel="noopener noreferrer"
              className="p-1.5 rounded-md hover:bg-surface-800 hover:text-text-primary transition-colors"
              aria-label="Share on Twitter"
            >
              <Globe size={16} />
            </a>
            <a
              href={`https://www.linkedin.com/shareArticle?mini=true&url=${encodeURIComponent(shareUrl)}&title=${encodeURIComponent(shareText)}`}
              target="_blank"
              rel="noopener noreferrer"
              className="p-1.5 rounded-md hover:bg-surface-800 hover:text-text-primary transition-colors"
              aria-label="Share on LinkedIn"
            >
              <LinkIcon size={16} />
            </a>
            <a
              href={`https://github.com/openzep/openzep`}
              target="_blank"
              rel="noopener noreferrer"
              className="p-1.5 rounded-md hover:bg-surface-800 hover:text-text-primary transition-colors"
              aria-label="Share on GitHub"
            >
              <GitFork size={16} />
            </a>
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
            <Button variant="primary" size="lg" icon={<ArrowDown size={18} />}>
              Download the latest version
            </Button>
          </a>
        </div>
      </div>
    </section>
  );
}
