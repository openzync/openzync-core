// ═══════════════════════════════════════════════════════════════════════════════
// OpenZync Landing — Changelog utilities
// ═══════════════════════════════════════════════════════════════════════════════

import fs from "fs";
import path from "path";
import type { ReactElement } from "react";
import { parseFrontmatter } from "@/lib/frontmatter";
import { compileMDX } from "next-mdx-remote/rsc";
import { useMDXComponents } from "@/app/mdx-components";

export interface ChangelogEntry {
  slug: string;
  title: string;
  date: string;
  version: string;
  excerpt: string;
  content: string;
}

/** A changelog entry ready to render in the detail page. */
export type ChangelogEntryDetail = Omit<ChangelogEntry, "content"> & {
  MDXContent: ReactElement;
};

const CHANGELOG_DIR = path.join(process.cwd(), "content/changelog");

/** Get all changelog entries (metadata only), sorted by date descending. */
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

/** Get a single changelog entry by slug, compiled as MDX. */
export async function getChangelogEntry(slug: string): Promise<ChangelogEntryDetail | null> {
  const filePath = path.join(CHANGELOG_DIR, `${slug}.mdx`);
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
    version: metadata.version ?? "",
    excerpt: metadata.excerpt ?? "",
    MDXContent,
  };
}
