import Link from "next/link";
import { ArrowRight, Calendar, Newspaper, GitCommit } from "lucide-react";
import { Badge } from "@openzep/design-system";
import { getAllChangelogEntries } from "@/lib/changelog";
import { getAllBlogPosts } from "@/lib/blog";
import { FormattedDate } from "./formatted-date";

/**
 * News section — Plone's "Latest News" pattern.
 * Shows mix of blog posts and changelog entries as cards
 * (image placeholder + category badge + title + date + "Read more").
 */
export function NewsSection() {
  const blogPosts = getAllBlogPosts().slice(0, 2);
  const changelogEntries = getAllChangelogEntries().slice(0, 1);

  const newsItems = [
    ...blogPosts.map((p) => ({
      title: p.title,
      date: p.date,
      excerpt: p.excerpt,
      href: `/blog/${p.slug}`,
      category: p.category,
      type: "blog" as const,
    })),
    ...changelogEntries.map((e) => ({
      title: e.title,
      date: e.date,
      excerpt: e.excerpt,
      href: `/changelog/${e.slug}`,
      category: e.version ? `v${e.version}` : "changelog",
      type: "changelog" as const,
    })),
  ];

  if (newsItems.length === 0) return null;

  return (
    <section className="py-20 md:py-28 border-t border-surface-800">
      <div className="mx-auto max-w-7xl px-6">
        <h2 className="text-3xl md:text-4xl font-bold tracking-tight text-center mb-12">
          Latest News
        </h2>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-6 max-w-5xl mx-auto">
          {newsItems.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className="card-interactive overflow-hidden flex flex-col"
            >
              {/* Image placeholder — themed icon */}
              <div className="h-40 bg-surface-800 flex items-center justify-center">
                {item.type === "blog" ? (
                  <Newspaper size={40} className="text-brand-500/30" />
                ) : (
                  <GitCommit size={40} className="text-brand-500/30" />
                )}
              </div>
              <div className="p-5 flex-1 flex flex-col">
                <div className="flex items-center gap-2 mb-3">
                  <Badge
                    variant={item.type === "blog" ? "info" : "brand"}
                    size="sm"
                  >
                    {item.type === "blog" ? item.category : item.category}
                  </Badge>
                  {item.date && (
                    <span className="flex items-center gap-1 text-[10px] text-surface-500">
                      <Calendar size={10} />
                      <FormattedDate date={item.date} />
                    </span>
                  )}
                </div>
                <h3 className="text-sm font-semibold text-text-primary mb-2 leading-snug">
                  {item.title}
                </h3>
                <p className="text-xs text-surface-400 leading-relaxed flex-1">
                  {item.excerpt}
                </p>
                <span className="inline-flex items-center gap-1 text-xs font-medium text-brand-300 hover:text-brand-200 transition-colors mt-3">
                  Read more
                  <ArrowRight size={12} />
                </span>
              </div>
            </Link>
          ))}
        </div>

        <div className="text-center mt-8">
          <Link
            href="/blog"
            className="inline-flex items-center gap-1.5 text-sm font-medium text-brand-300 hover:text-brand-200 transition-colors"
          >
            All news
            <ArrowRight size={14} />
          </Link>
        </div>
      </div>
    </section>
  );
}
