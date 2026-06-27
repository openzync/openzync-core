import type { MetadataRoute } from "next";
import { siteConfig } from "@/content/site-config";
import { getAllBlogPosts } from "@/lib/blog";
import { getAllChangelogEntries } from "@/lib/changelog";

/**
 * Dynamic sitemap generation.
 * Includes all static routes and dynamic blog/changelog entries.
 */
export default function sitemap(): MetadataRoute.Sitemap {
  const baseUrl = siteConfig.url;

  // Static routes with their change frequency and priority
  const staticRoutes: MetadataRoute.Sitemap = [
    { url: baseUrl, changeFrequency: "weekly", priority: 1.0 },
    { url: `${baseUrl}/features`, changeFrequency: "monthly", priority: 0.8 },
    { url: `${baseUrl}/about`, changeFrequency: "monthly", priority: 0.7 },
    { url: `${baseUrl}/blog`, changeFrequency: "weekly", priority: 0.8 },
    { url: `${baseUrl}/changelog`, changeFrequency: "weekly", priority: 0.6 },
    { url: `${baseUrl}/use-cases`, changeFrequency: "monthly", priority: 0.7 },
    { url: `${baseUrl}/faq`, changeFrequency: "monthly", priority: 0.6 },
    { url: `${baseUrl}/events`, changeFrequency: "weekly", priority: 0.7 },
    { url: `${baseUrl}/press-kit`, changeFrequency: "monthly", priority: 0.3 },
    { url: `${baseUrl}/privacy`, changeFrequency: "yearly", priority: 0.3 },
    { url: `${baseUrl}/terms`, changeFrequency: "yearly", priority: 0.3 },
  ];

  // Blog posts
  const blogPosts = getAllBlogPosts().map((post) => ({
    url: `${baseUrl}/blog/${post.slug}`,
    changeFrequency: "monthly" as const,
    priority: 0.6,
    lastModified: new Date(post.date),
  }));

  // Changelog entries
  const changelogEntries = getAllChangelogEntries().map((entry) => ({
    url: `${baseUrl}/changelog/${entry.slug}`,
    changeFrequency: "monthly" as const,
    priority: 0.5,
    lastModified: new Date(entry.date),
  }));

  return [...staticRoutes, ...blogPosts, ...changelogEntries];
}
