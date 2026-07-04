"use client";

import { useState } from "react";
import { cn } from "@/lib/utils";
import { audienceTabs } from "@/content/audience-tabs";
import { ArrowRight } from "lucide-react";
import Link from "next/link";

/**
 * Audience tabs section — per-audience content panels
 * (Developers, Tech Leads, Product Managers) with contextual code snippets.
 */

/** Per-tab contextual code snippets shown at the bottom of each panel. */
const panelSnippets: Record<string, { lines: { text: string; highlight?: string }[] }> = {
  developers: {
    lines: [
      { text: "$ pip install openzync" },
      { text: ">>> from openzync import AgentMemory" },
      { text: '>>> memory = AgentMemory(backend="neo4j")', highlight: "AgentMemory" },
    ],
  },
  "tech-leads": {
    lines: [
      { text: "# config/agents.yaml" },
      { text: "backends:", highlight: "backends" },
      { text: '  - type: "neo4j"'},
      { text: '  - type: "falkordb"'},
    ],
  },
  product: {
    lines: [
      { text: "# usage analytics" },
      { text: "requests: 10,432", highlight: "10,432" },
      { text: "p95 latency: 42ms", highlight: "42ms" },
      { text: "active sessions: 2,847", highlight: "2,847" },
    ],
  },
};

export function AudienceTabs() {
  const [activeTab, setActiveTab] = useState(audienceTabs[0]?.key ?? "");

  const currentTab = audienceTabs.find((t) => t.key === activeTab);
  if (!currentTab) return null;

  const snippet = panelSnippets[activeTab];

  return (
    <section className="py-20 md:py-28 border-t border-surface-800">
      <div className="mx-auto max-w-7xl px-6">
        <div className="max-w-4xl mx-auto">
          <h2 className="text-3xl md:text-4xl font-bold tracking-tight text-center mb-2">
            OpenZync for
          </h2>

          {/* Tab buttons */}
          <div className="flex justify-center gap-1 mb-10 mt-8">
            {audienceTabs.map((tab) => (
              <button
                key={tab.key}
                onClick={() => setActiveTab(tab.key)}
                className={cn(
                  "rounded-lg px-5 py-2.5 text-sm font-medium transition-all",
                  activeTab === tab.key
                    ? "bg-brand-500 text-white"
                    : "text-surface-400 hover:text-text-primary hover:bg-surface-800",
                )}
              >
                {tab.label}
              </button>
            ))}
          </div>

          {/* Tab content panel */}
          <div className="card-base p-8 md:p-10">
            <h3 className="text-xl md:text-2xl font-bold text-text-primary mb-4">
              {currentTab.heading}
            </h3>
            <p className="text-surface-400 leading-relaxed mb-6">
              {currentTab.description}
            </p>
            <ul className="space-y-3 mb-8">
              {currentTab.links.map((link) => (
                <li key={link.label}>
                  {link.href.startsWith("http") ? (
                    <a
                      href={link.href}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-2 text-sm text-brand-300 hover:text-brand-200 transition-colors"
                    >
                      <span className="h-1.5 w-1.5 rounded-full bg-brand-500 shrink-0" />
                      {link.label}
                    </a>
                  ) : (
                    <Link
                      href={link.href}
                      className="inline-flex items-center gap-2 text-sm text-brand-300 hover:text-brand-200 transition-colors"
                    >
                      <span className="h-1.5 w-1.5 rounded-full bg-brand-500 shrink-0" />
                      {link.label}
                    </Link>
                  )}
                </li>
              ))}
            </ul>

            {/* Per-tab contextual code snippet */}
            {snippet && (
              <div className="rounded-lg border border-surface-700/50 bg-surface-900 overflow-hidden">
                <div className="flex items-center gap-1 px-4 py-1.5 bg-surface-800/60 border-b border-surface-700/50">
                  <span className="h-2 w-2 rounded-full bg-red-500/60" />
                  <span className="h-2 w-2 rounded-full bg-yellow-500/60" />
                  <span className="h-2 w-2 rounded-full bg-green-500/60" />
                  <span className="ml-2 text-[10px] text-surface-600 font-mono">snippet.py</span>
                </div>
                <div className="px-4 py-3 font-mono text-[12px] leading-relaxed">
                  {snippet.lines.map((line, i) => (
                    <div key={i}>
                      {line.highlight ? (
                        <>
                          {line.text.substring(0, line.text.indexOf(line.highlight))}
                          <span className="text-brand-300">{line.highlight}</span>
                          {line.text.substring(line.text.indexOf(line.highlight) + line.highlight.length)}
                        </>
                      ) : (
                        <span>{line.text}</span>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
