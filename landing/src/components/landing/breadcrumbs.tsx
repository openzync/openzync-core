import Link from "next/link";
import { ChevronRight } from "lucide-react";
import { breadcrumbLabels } from "@/content/site-config";

interface BreadcrumbsProps {
  /** Path segments after the base path, e.g. ["features"] */
  segments: { label: string; href: string }[];
}

/**
 * Breadcrumb navigation following Plone's pattern.
 * Renders as: Home > Section > Page with muted separators.
 */
export function Breadcrumbs({ segments }: BreadcrumbsProps) {
  if (segments.length === 0) return null;

  return (
    <nav aria-label="Breadcrumb" className="mb-8">
      <ol className="flex flex-wrap items-center gap-1.5 text-sm text-surface-500">
        <li>
          <Link href="/" className="hover:text-brand-300 transition-colors">
            Home
          </Link>
        </li>
        {segments.map((seg, i) => (
          <li key={seg.href} className="flex items-center gap-1.5">
            <ChevronRight size={14} className="text-surface-700" />
            {i === segments.length - 1 ? (
              <span className="text-surface-300 font-medium" aria-current="page">
                {seg.label}
              </span>
            ) : (
              <Link href={seg.href} className="hover:text-brand-300 transition-colors">
                {seg.label}
              </Link>
            )}
          </li>
        ))}
      </ol>
    </nav>
  );
}

/**
 * Build breadcrumb segments from a pathname.
 * Looks up labels from breadcrumbLabels config.
 */
export function buildBreadcrumbSegments(pathname: string): { label: string; href: string }[] {
  const parts = pathname.split("/").filter(Boolean);
  const segments: { label: string; href: string }[] = [];

  let accumulated = "";
  for (const part of parts) {
    accumulated += `/${part}`;
    const label = breadcrumbLabels[accumulated] ?? part.replace(/-/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
    segments.push({ label, href: accumulated });
  }

  return segments;
}
