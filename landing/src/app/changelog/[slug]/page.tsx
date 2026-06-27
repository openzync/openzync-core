import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { Badge } from "@openzep/design-system";
import { ChevronLeft, Calendar } from "lucide-react";
import { getChangelogEntry, getAllChangelogEntries } from "@/lib/changelog";
import { MarkdownContent } from "@/components/landing/markdown-content";

interface Props {
  params: Promise<{ slug: string }>;
}

/** Generate static params for all changelog entries */
export async function generateStaticParams() {
  const entries = getAllChangelogEntries();
  return entries.map((entry) => ({ slug: entry.slug }));
}

/** Generate metadata for the page */
export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { slug } = await params;
  const entry = getChangelogEntry(slug);
  if (!entry) return {};

  return {
    title: entry.title,
    description: entry.excerpt,
  };
}

export default async function ChangelogEntryPage({ params }: Props) {
  const { slug } = await params;
  const entry = getChangelogEntry(slug);
  if (!entry) notFound();

  return (
    <div className="pt-36 pb-20">
      <div className="mx-auto max-w-3xl px-6">
        {/* Back link */}
        <Link
          href="/changelog"
          className="inline-flex items-center gap-1 text-sm text-surface-400 hover:text-text-primary transition-colors mb-8"
        >
          <ChevronLeft size={16} />
          Back to changelog
        </Link>

        {/* Entry header */}
        <div className="mb-8">
          <div className="flex items-center gap-3 mb-3">
            {entry.version && (
              <Badge variant="brand" size="md">
                v{entry.version}
              </Badge>
            )}
            {entry.date && (
              <span className="flex items-center gap-1 text-xs text-surface-500">
                <Calendar size={12} />
                {new Date(entry.date).toLocaleDateString("en-US", {
                  year: "numeric",
                  month: "long",
                  day: "numeric",
                })}
              </span>
            )}
          </div>
          <h1 className="text-3xl md:text-4xl font-extrabold tracking-tight">
            {entry.title}
          </h1>
        </div>

        {/* Entry content */}
        <article className="prose-custom">
          <MarkdownContent content={entry.content} />
        </article>
      </div>
    </div>
  );
}
