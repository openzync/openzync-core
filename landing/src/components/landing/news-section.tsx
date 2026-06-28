import Link from "next/link";
import { ArrowRight } from "lucide-react";
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
              {/* Card header — git-log style */}
              <div className="h-32 bg-surface-900 border-b border-surface-800 flex items-center px-5">
                <div className="font-mono text-[11px] leading-relaxed w-full">
                  <div className="flex items-center gap-1.5">
                    <span className="text-brand-500/60 font-semibold">
                      {item.type === "blog" ? "feat" : "chore"}
                    </span>
                    <span className="text-surface-600">(</span>
                    <span className="text-accent-400">{item.type === "blog" ? "blog" : "release"}</span>
                    <span className="text-surface-600">):</span>
                  </div>
                  <div className="text-surface-300 truncate w-full pl-4 border-l-2 border-surface-700 mt-0.5">
                    {item.type === "blog" ? item.title : item.category}
                  </div>
                  {item.date && (
                    <div className="text-surface-600 text-[10px] pl-4 mt-0.5">
                      Date: <FormattedDate date={item.date} />
                    </div>
                  )}
                </div>
              </div>
              <div className="p-5 flex-1 flex flex-col">
                <div className="flex items-center gap-2 mb-3">
                  <Badge
                    variant={item.type === "blog" ? "info" : "brand"}
                    size="sm"
                  >
                    {item.category}
                  </Badge>
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
