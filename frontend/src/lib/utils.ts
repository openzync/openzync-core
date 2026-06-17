// ═══════════════════════════════════════════════════════════════════════════════
// OpenZep — Shared Utilities
// ═══════════════════════════════════════════════════════════════════════════════

import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/** Merge Tailwind classes with conflict resolution */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

// ─── Date / Time formatting ──────────────────────────────────────────────────

/** Relative time string ("3m ago", "2h ago", "just now") */
export function timeAgo(dateStr: string | null | undefined): string {
  if (!dateStr) return "—";
  const seconds = Math.floor((Date.now() - new Date(dateStr).getTime()) / 1000);
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const mins = Math.floor(seconds / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

/** Formatted date string ("Apr 15, 2025") */
export function formatDate(
  dateStr: string | null | undefined,
  withTime = false,
): string {
  if (!dateStr) return "—";
  const d = new Date(dateStr);
  const opts: Intl.DateTimeFormatOptions = {
    month: "short",
    day: "numeric",
    year: "numeric",
  };
  if (withTime) {
    opts.hour = "2-digit";
    opts.minute = "2-digit";
  }
  return d.toLocaleDateString("en-US", opts);
}

/** Smart timestamp: "just now" for recent, "Mon 14:32" for today, etc. */
export function smartTimestamp(dateStr: string): string {
  const d = new Date(dateStr);
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  const diffMins = Math.floor(diffMs / 60_000);

  if (diffMins < 1) return "just now";
  if (diffMins < 60) return `${diffMins}m ago`;

  const timeStr = d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
  const dateStr_fmt = d.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
  });

  if (d.toDateString() === now.toDateString()) return timeStr;
  if (d.getFullYear() === now.getFullYear())
    return `${dateStr_fmt} ${timeStr}`;
  return `${dateStr_fmt} ${d.getFullYear()} ${timeStr}`;
}

// ─── Text / ID helpers ───────────────────────────────────────────────────────

/** Truncate a UUID or long ID to first N characters */
export function truncateId(id: string | null | undefined, chars = 8): string {
  if (!id) return "—";
  return id.length > chars ? id.slice(0, chars) : id;
}

/** Copy text to clipboard and return success status */
export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/**
 * Extract a human-readable label from an action string.
 * e.g. "session.create" → "Session created"
 */
export function actionLabel(action: string): string {
  const map: Record<string, string> = {
    "session.create": "Session created",
    "session.delete": "Session deleted",
    "user.create": "User created",
    "user.delete": "User deleted",
    "memory.ingest": "Memory ingested",
    "api_key.create": "API key created",
    "api_key.revoke": "API key revoked",
  };
  return (
    map[action] ??
    action
      .replace(/_/g, " ")
      .replace(/\b\w/g, (c) => c.toUpperCase())
  );
}

// ─── Number helpers ───────────────────────────────────────────────────────────

/** Format a number with locale separators (e.g. 1,234) */
export function formatNumber(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return n.toLocaleString();
}
