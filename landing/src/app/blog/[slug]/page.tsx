import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { Badge } from "@openzep/design-system";
import { ChevronLeft, Calendar, User, Share2, Globe, Link as LinkIcon } from "lucide-react";
import { getBlogPost, getAllBlogPosts } from "@/lib/blog";
import { MarkdownContent } from "@/components/landing/markdown-content";
import { Breadcrumbs, buildBreadcrumbSegments } from "@/components/landing/breadcrumbs";

const shareUrl = "https://openzep.com";

interface Props {
  params: Promise<{ slug: string }>;
}

/** Generate static params for all blog posts */
export async function generateStaticParams() {
  const posts = getAllBlogPosts();
  return posts.map((post) => ({ slug: post.slug }));
}

/** Generate metadata for the page */
export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { slug } = await params;
  const post = getBlogPost(slug);
  if (!post) return {};

  return {
    title: post.title,
    description: post.excerpt,
  };
}

export default async function BlogPostPage({ params }: Props) {
  const { slug } = await params;
  const post = getBlogPost(slug);
  if (!post) notFound();

  const segments = buildBreadcrumbSegments(`/blog/${slug}`);

  return (
    <div className="pt-28 pb-20">
      <div className="mx-auto max-w-3xl px-6">
        <Breadcrumbs segments={segments} />

        {/* Back link */}
        <Link
          href="/blog"
          className="inline-flex items-center gap-1 text-sm text-surface-400 hover:text-text-primary transition-colors mb-8"
        >
          <ChevronLeft size={16} />
          Back to blog
        </Link>

        {/* Post header */}
        <div className="mb-8">
          <div className="flex items-center gap-3 mb-3">
            {post.category && (
              <Badge variant="info" size="md">
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
            {post.author && (
              <span className="flex items-center gap-1 text-xs text-surface-500">
                <User size={12} />
                {post.author}
              </span>
            )}
          </div>
          <h1 className="text-3xl md:text-4xl font-extrabold tracking-tight">
            {post.title}
          </h1>
        </div>

        {/* Social share (Plone pattern) */}
        <div className="flex items-center gap-2 mb-8 text-surface-500">
          <span className="text-xs flex items-center gap-1">
            <Share2 size={12} />
            Share:
          </span>
          <a
            href={`https://twitter.com/intent/tweet?url=${encodeURIComponent(`${shareUrl}/blog/${slug}`)}&text=${encodeURIComponent(post.title)}`}
            target="_blank"
            rel="noopener noreferrer"
            className="p-1.5 rounded-md hover:bg-surface-800 hover:text-text-primary transition-colors"
            aria-label="Share on Twitter"
          >
            <Globe size={16} />
          </a>
          <a
            href={`https://www.linkedin.com/shareArticle?mini=true&url=${encodeURIComponent(`${shareUrl}/blog/${slug}`)}&title=${encodeURIComponent(post.title)}`}
            target="_blank"
            rel="noopener noreferrer"
            className="p-1.5 rounded-md hover:bg-surface-800 hover:text-text-primary transition-colors"
            aria-label="Share on LinkedIn"
          >
            <LinkIcon size={16} />
          </a>
        </div>

        {/* Post content */}
        <article className="prose-custom">
          <MarkdownContent content={post.content} />
        </article>
      </div>
    </div>
  );
}
