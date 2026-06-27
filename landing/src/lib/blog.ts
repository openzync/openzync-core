// ═══════════════════════════════════════════════════════════════════════════════
// OpenZep Landing — Blog utilities
// ═══════════════════════════════════════════════════════════════════════════════

import fs from "fs";
import path from "path";
import type { BlogPost } from "@/content/blog";

const BLOG_DIR = path.join(process.cwd(), "content/blog");

/**
 * Parse frontmatter from MDX content manually.
 * Supports: title, date, excerpt, author, category
 */
function parseFrontmatter(fileContent: string): {
  metadata: Record<string, string>;
  content: string;
} {
  const metadata: Record<string, string> = {};
  let content = fileContent;

  if (content.startsWith("---")) {
    const endIndex = content.indexOf("---", 3);
    if (endIndex !== -1) {
      const frontmatter = content.slice(3, endIndex).trim();
      content = content.slice(endIndex + 3).trim();

      for (const line of frontmatter.split("\n")) {
        const colonIndex = line.indexOf(":");
        if (colonIndex !== -1) {
          const key = line.slice(0, colonIndex).trim();
          const value = line.slice(colonIndex + 1).trim().replace(/^"|"$/g, "");
          metadata[key] = value;
        }
      }
    }
  }

  return { metadata, content };
}

/** Get all blog posts, sorted by date descending. */
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
      author: metadata.author ?? "OpenZep Team",
      category: metadata.category ?? "community",
      content,
    });
  }

  posts.sort((a, b) => new Date(b.date).getTime() - new Date(a.date).getTime());
  return posts;
}

/** Get a single blog post by slug. */
export function getBlogPost(slug: string): BlogPost | null {
  const filePath = path.join(BLOG_DIR, `${slug}.mdx`);
  if (!fs.existsSync(filePath)) return null;

  const raw = fs.readFileSync(filePath, "utf-8");
  const { metadata, content } = parseFrontmatter(raw);

  return {
    slug,
    title: metadata.title ?? slug,
    date: metadata.date ?? "",
    excerpt: metadata.excerpt ?? "",
    author: metadata.author ?? "OpenZep Team",
    category: metadata.category ?? "community",
    content,
  };
}
