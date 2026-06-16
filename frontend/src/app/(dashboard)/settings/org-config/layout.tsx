"use client";

import { usePathname } from "next/navigation";
import Link from "next/link";
import { cn } from "@/lib/utils";

const TABS = [
  { label: "LLM", href: "/settings/org-config/llm", id: "llm" },
  { label: "Embeddings", href: "/settings/org-config/embeddings", id: "embeddings" },
  { label: "Graph", href: "/settings/org-config/graph", id: "graph" },
  { label: "Behaviour", href: "/settings/org-config/behaviour", id: "behaviour" },
];

export default function OrgConfigLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Organization Configuration</h1>
        <p className="text-sm text-surface-400 mt-1">
          Manage settings for LLM, embeddings, graph, and behaviour
        </p>
      </div>

      {/* Tab bar */}
      <div className="flex gap-1 p-1 card-base w-fit">
        {TABS.map((tab) => {
          const isActive = pathname === tab.href;
          return (
            <Link
              key={tab.id}
              href={tab.href}
              className={cn(
                "px-3 py-1.5 text-sm rounded-md transition-colors",
                isActive
                  ? "bg-brand-500 text-white"
                  : "text-surface-400 hover:text-white hover:bg-surface-800",
              )}
            >
              {tab.label}
            </Link>
          );
        })}
      </div>

      {/* Active tab content */}
      {children}
    </div>
  );
}
