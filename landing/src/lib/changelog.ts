// ═══════════════════════════════════════════════════════════════════════════════
// OpenZep Landing — Changelog utilities
// ═══════════════════════════════════════════════════════════════════════════════

import fs from "fs";
import path from "path";

export interface ChangelogEntry {
  slug: string;
  title: string;
  date: string;
  version: string;
  excerpt: string;
  content: string;
}

const CHANGELOG_DIR = path.join(process.cwd(), "content/changelog");

/**
 * Parse frontmatter from MDX content manually (avoids gray-matter dependency).
 * Supports: title, date, version, excerpt
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

/** Get all changelog entries, sorted by date descending. */
export function getAllChangelogEntries(): ChangelogEntry[] {
  if (!fs.existsSync(CHANGELOG_DIR)) {
    return [];
  }

  const files = fs.readdirSync(CHANGELOG_DIR);

  const entries: ChangelogEntry[] = [];

  for (const file of files) {
    if (!file.endsWith(".mdx")) continue;

    const slug = file.replace(/\.mdx$/, "");
    const filePath = path.join(CHANGELOG_DIR, file);
    const raw = fs.readFileSync(filePath, "utf-8");
    const { metadata, content } = parseFrontmatter(raw);

    entries.push({
      slug,
      title: metadata.title ?? slug,
      date: metadata.date ?? "",
      version: metadata.version ?? "",
      excerpt: metadata.excerpt ?? content.slice(0, 200).replace(/[#*`]/g, "").trim(),
      content,
    });
  }

  // Sort by date descending (most recent first)
  entries.sort((a, b) => new Date(b.date).getTime() - new Date(a.date).getTime());

  return entries;
}

/** Get a single changelog entry by slug. */
export function getChangelogEntry(slug: string): ChangelogEntry | null {
  const filePath = path.join(CHANGELOG_DIR, `${slug}.mdx`);
  if (!fs.existsSync(filePath)) return null;

  const raw = fs.readFileSync(filePath, "utf-8");
  const { metadata, content } = parseFrontmatter(raw);

  return {
    slug,
    title: metadata.title ?? slug,
    date: metadata.date ?? "",
    version: metadata.version ?? "",
    excerpt: metadata.excerpt ?? "",
    content,
  };
}
