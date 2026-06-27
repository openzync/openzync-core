import type { Metadata } from "next";
import Link from "next/link";
import { Breadcrumbs, buildBreadcrumbSegments } from "@/components/landing/breadcrumbs";
import { Badge } from "@openzep/design-system";
import { Calendar, ArrowRight } from "lucide-react";
import { getAllBlogPosts } from "@/lib/blog";

export const metadata: Metadata = {
  title: "Blog",
  description: "Latest news, engineering deep-dives, tutorials, and community updates from OpenZep.",
};

export default function BlogPage() {
  const posts = getAllBlogPosts();
  const segments = buildBreadcrumbSegments("/blog");

  return (
    <div className="pt-36 pb-20">
      <div className="mx-auto max-w-4xl px-6">
        <Breadcrumbs segments={segments} />

        {/* Hero banner (Plone news page pattern) */}
        <div className="card-base p-8 mb-12 bg-gradient-to-br from-brand-500/5 to-surface-900">
          <h1 className="text-3xl md:text-4xl font-extrabold tracking-tight mb-3">
            Blog
          </h1>
          <p className="text-surface-400 max-w-xl">
            Engineering deep-dives, community updates, release announcements, and tutorials
            from the OpenZep team.
          </p>
        </div>

        {/* Posts grid */}
        {posts.length === 0 ? (
          <div className="text-center py-20">
            <p className="text-surface-500">No blog posts yet. Check back soon!</p>
          </div>
        ) : (
          <div className="space-y-6">
            {posts.map((post) => (
              <Link
                key={post.slug}
                href={`/blog/${post.slug}`}
                className="card-interactive block p-6"
              >
                <div className="flex items-center gap-3 mb-3">
                  {post.category && (
                    <Badge variant="info" size="sm">
                      {post.category}
                    </Badge>
                  )}
                  {post.date && (
                    <span className="flex items-center gap-1 text-xs text-surface-500">
                      <Calendar size={12} />
                      {new Date(post.date).toLocaleDateString("en-US", {
                        year: "numeric",
                        month: "long",
                        day: "numeric",
                      })}
                    </span>
                  )}
                </div>
                <h2 className="text-xl font-semibold text-text-primary mb-2">
                  {post.title}
                </h2>
                <p className="text-sm text-surface-400 leading-relaxed mb-3">
                  {post.excerpt}
                </p>
                <span className="inline-flex items-center gap-1 text-sm font-medium text-brand-300">
                  Read more
                  <ArrowRight size={14} />
                </span>
              </Link>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
