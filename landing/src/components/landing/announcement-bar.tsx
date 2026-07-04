"use client";

import { useState, useEffect } from "react";
import { X } from "lucide-react";

const STORAGE_KEY = "openzync-announcement-dismissed";
const BAR_HEIGHT = 36;

/**
 * Fixed announcement bar at the very top of the page.
 * - Shows "Now available in public beta" with a pulsing dot
 * - Dismissable — state persisted in localStorage
 * - Sets --announcement-height CSS variable for the navbar to consume
 */
export function AnnouncementBar() {
  const [dismissed, setDismissed] = useState(true);
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored !== "true") {
      setDismissed(false);
    }
  }, []);

  // Sync CSS variable so the navbar can read it
  useEffect(() => {
    document.documentElement.style.setProperty(
      "--announcement-height",
      dismissed ? "0px" : `${BAR_HEIGHT}px`,
    );
  }, [dismissed]);

  const handleDismiss = () => {
    setDismissed(true);
    localStorage.setItem(STORAGE_KEY, "true");
  };

  // Don't render during SSR to avoid flash
  if (!mounted || dismissed) return null;

  return (
    <div className="fixed top-0 left-0 right-0 z-50 h-9 bg-surface-900 border-b border-brand-500/20 flex items-center justify-center gap-2 text-xs">
      <span className="h-1.5 w-1.5 rounded-full bg-success animate-pulse-dot shrink-0" />
      <span className="text-accent-300 font-medium">
        Now available in public beta
      </span>
      <button
        onClick={handleDismiss}
        className="absolute right-3 flex items-center justify-center rounded-md p-1 text-surface-400 hover:text-text-primary hover:bg-surface-800 transition-colors"
        aria-label="Dismiss announcement"
      >
        <X size={14} />
      </button>
    </div>
  );
}
