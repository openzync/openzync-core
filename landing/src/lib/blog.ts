// ═══════════════════════════════════════════════════════════════════════════════
// OpenZync Landing — Blog utilities
// ═══════════════════════════════════════════════════════════════════════════════

import fs from "fs";
import path from "path";
import type { ReactElement } from "react";
import type { BlogPost } from "@/content/blog";
import { parseFrontmatter } from "@/lib/frontmatter";
import { compileMDX } from "next-mdx-remote/rsc";
import { useMDXComponents } from "@/app/mdx-components";

const BLOG_DIR = path.join(process.cwd(), "content/blog");

/** A blog post ready to render in the detail page. */
export type BlogPostDetail = Omit<BlogPost, "content"> & {
  MDXContent: ReactElement;
};

/** Get all blog posts (metadata only), sorted by date descending. */
export function getAllBlogPosts(): BlogPost[] {
  if (!fs.existsSync(BLOG_DIR)) return [];

  const files = fs.readdirSync(BLOG_DIR);
  const posts: BlogPost[] = [];

  for (const file of files) {
    if (!file.endsWith(".mdx")) continue;

    const slug = file.replace(/\.mdx$/, "");
    const filePath = path.join(BLOG_DIR, file);
    const raw = fs.readFileSync(filePath, "utf-8");
    const { metadata, content } = parseFrontmatter(raw);

    posts.push({
      slug,
      title: metadata.title ?? slug,
      date: metadata.date ?? "",
      excerpt: metadata.excerpt ?? content.slice(0, 200).replace(/[#*`]/g, "").trim(),
      author: metadata.author ?? "OpenZync Team",
      category: metadata.category ?? "community",
      content,
    });
  }

  posts.sort((a, b) => new Date(b.date).getTime() - new Date(a.date).getTime());
  return posts;
}

/** Get a single blog post by slug, compiled as MDX. */
export async function getBlogPost(slug: string): Promise<BlogPostDetail | null> {
  const filePath = path.join(BLOG_DIR, `${slug}.mdx`);
  if (!fs.existsSync(filePath)) return null;

  const raw = fs.readFileSync(filePath, "utf-8");
  const { metadata, content } = parseFrontmatter(raw);

  const { content: MDXContent } = await compileMDX({
    source: content,
    components: useMDXComponents({}),
    options: { parseFrontmatter: false },
  });

  return {
    slug,
    title: metadata.title ?? slug,
    date: metadata.date ?? "",
    excerpt: metadata.excerpt ?? "",
    author: metadata.author ?? "OpenZync Team",
    category: metadata.category ?? "community",
    MDXContent,
  };
}
