"use client";

import Link from "next/link";
import { cn } from "@/lib/utils";

interface SessionTabsProps {
  sessionId: string;
  userId: string;
  activeTab: "messages" | "facts" | "graph" | "classifications" | "extractions";
}

const TABS = [
  { id: "messages", label: "Messages", href: "messages" },
  { id: "facts", label: "Facts", href: "facts" },
  { id: "graph", label: "Graph", href: "graph" },
  { id: "classifications", label: "Classifications", href: "classifications" },
  { id: "extractions", label: "Extractions", href: "extractions" },
] as const;

export default function SessionTabs({ sessionId, userId, activeTab }: SessionTabsProps) {
  return (
    <div className="mb-4 border-b border-surface-800">
      <nav className="flex gap-0 -mb-px">
        {TABS.map((tab) => {
          const isActive = tab.id === activeTab;
          return (
            <Link
              key={tab.id}
              href={`/sessions/${sessionId}/${tab.href}?userId=${userId}`}
              className={cn(
                "px-4 py-3 text-sm font-medium border-b-2 transition-colors whitespace-nowrap",
                isActive
                  ? "text-brand-500 border-brand-500"
                  : "text-surface-400 border-transparent hover:text-surface-200 hover:border-surface-600",
              )}
            >
              {tab.label}
            </Link>
          );
        })}
      </nav>
    </div>
  );
}
