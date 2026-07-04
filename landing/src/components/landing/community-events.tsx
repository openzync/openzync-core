"use client";

import { useState } from "react";
import { ChevronRight } from "lucide-react";

/**
 * Release timeline section — upcoming releases shown as a git-branch road layout.
 * Desktop: vertical trunk on the left with horizontal branch lanes extending right.
 *   Each lane has a dashed branch line with staggered commit dots (newest = farthest right),
 *   expandable on click to reveal git commit metadata.
 * Mobile: simple vertical list with left commit-dot rail.
 */
export function CommunityEvents() {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const toggleExpanded = (version: string) => {
    setExpanded((prev) => ({ ...prev, [version]: !prev[version] }));
  };

  const milestones = [
    {
      version: "v1.0",
      hash: "0000000",
      title: "Memory Graph Foundation",
      scope: "core",
      date: "Q1 2026",
      description:
        "Core memory graph engine with vector storage, entity resolution, and multi-tenant isolation for production deployments.",
    },
    {
      version: "v1.1",
      hash: "a1b2c3d",
      title: "Multi-Model Orchestration",
      scope: "multi-llm",
      date: "Q3 2026",
      description:
        "Simultaneous multi-LLM routing with automatic failover and cost optimization across providers.",
    },
    {
      version: "v1.2",
      hash: "e4f5g6h",
      title: "Advanced Observability Suite",
      scope: "obs",
      date: "Q1 2027",
      description:
        "Real-time memory graph visualization, query profiling, and custom metrics export to Prometheus/Grafana.",
    },
    {
      version: "v2.0",
      hash: "i7j8k9l",
      title: "Distributed Memory Grid",
      scope: "grid",
      date: "Q3 2027",
      description:
        "Horizontally scalable memory clusters with cross-region replication and sub-millisecond query latency.",
    },
    {
      version: "v3.0",
      hash: "m0n1p2q",
      title: "Autonomous Agent Swarm",
      scope: "swarm",
      date: "2028",
      description:
        "Self-organizing agent collectives with dynamic memory pooling, automatic tool discovery, and emergent workflow optimization.",
    },
  ];

  // Dashed line widths (percentages) — staggered dots from oldest (shortest dash) to newest (longest dash)
  const dashWidths = [24, 30, 36, 42, 48];

  return (
    <section className="py-20 md:py-28 border-t border-surface-800">
      <div className="mx-auto max-w-7xl px-6">
        <div className="text-center mb-2">
          <span className="font-mono text-[11px] text-surface-600">
            # upcoming releases
          </span>
        </div>
        <h2 className="text-3xl md:text-4xl font-bold tracking-tight text-center mb-16">
          What&apos;s Next
        </h2>

        {/* ── Timeline container ──────────────────────────────────────────── */}
        <div className="relative max-w-5xl mx-auto">
          {/* --- Single vertical trunk line (desktop) --- */}
          <div className="absolute left-[38px] top-0 bottom-0 w-px bg-surface-700 hidden lg:block z-0" />

          {/* --- "main" header (desktop) --- */}
          <div className="hidden lg:flex items-center gap-3 pb-2">
            <div className="w-[76px] shrink-0 flex items-center justify-center">
              <div className="w-3 h-3 rounded-full border-2 border-brand-500 bg-surface-950 z-10" />
            </div>
            <span className="font-mono text-[11px] text-surface-500 tracking-tight">
              main
            </span>
            <div className="h-px flex-1 bg-surface-700/40" />
          </div>

          {/* --- Lanes --- */}
          {milestones.map((item, idx) => {
            const isExpanded = !!expanded[item.version];

            return (
              <div key={item.version}>
                {/* ═══ Desktop lane ════════════════════════════════════════ */}
                <div
                  className="hidden lg:flex items-center gap-3 py-3 cursor-pointer select-none group"
                  onClick={() => toggleExpanded(item.version)}
                  role="button"
                  tabIndex={0}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      toggleExpanded(item.version);
                    }
                  }}
                  aria-expanded={isExpanded}
                >
                  {/* Left column: trunk dot + fork connector */}
                  <div className="w-[76px] shrink-0 relative flex items-center justify-center">
                    {/* Fork connector line (from trunk to branch name) */}
                    <div className="absolute right-0 left-1/2 h-px bg-surface-700" />
                    {/* Trunk commit dot */}
                    <div className="w-3 h-3 rounded-full border-2 border-brand-500 bg-surface-950 z-10" />
                  </div>

                  {/* Branch name */}
                  <span className="font-mono text-[11px] text-brand-300 font-semibold shrink-0 w-[100px]">
                    feature/{item.version}
                  </span>

                  {/* Dashed branch line */}
                  <div
                    className="h-px border-t-2 border-dashed border-surface-600/40 shrink-0"
                    style={{ width: `${dashWidths[idx]}%` }}
                  />

                  {/* Branch commit dot */}
                  <div className="w-3 h-3 rounded-full bg-brand-500/40 shrink-0 -ml-px" />

                  {/* Title + date + chevron */}
                  <div className="flex items-center gap-3 flex-1 min-w-0 ml-2">
                    <span className="text-sm font-semibold text-text-primary truncate">
                      {item.title}
                    </span>
                    <span className="text-xs text-surface-500 font-mono shrink-0">
                      {item.date}
                    </span>
                    <ChevronRight
                      size={16}
                      className={`shrink-0 text-surface-500 transition-transform duration-300 ml-auto ${
                        isExpanded ? "rotate-90" : ""
                      }`}
                    />
                  </div>
                </div>

                {/* ═══ Mobile lane ═════════════════════════════════════════ */}
                <div
                  className="lg:hidden flex items-start gap-3 py-2.5 cursor-pointer select-none group"
                  onClick={() => toggleExpanded(item.version)}
                  role="button"
                  tabIndex={0}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      toggleExpanded(item.version);
                    }
                  }}
                  aria-expanded={isExpanded}
                >
                  {/* Commit dot + vertical rail */}
                  <div className="flex flex-col items-center pt-1">
                    <div className="w-3 h-3 rounded-full border-2 border-brand-500 bg-surface-950 shrink-0" />
                    {idx < milestones.length - 1 && (
                      <div className="w-px flex-1 bg-surface-700 min-h-[20px]" />
                    )}
                  </div>

                  {/* Version badge + title + chevron */}
                  <div className="flex items-center gap-2 flex-1 min-w-0">
                    <span className="inline-flex items-center rounded-full border border-brand-500/30 bg-brand-500/10 px-2 py-0.5 text-[10px] font-semibold text-brand-300 shrink-0">
                      {item.version}
                    </span>
                    {isExpanded && (
                      <span className="text-sm font-semibold text-text-primary truncate">
                        {item.title}
                      </span>
                    )}
                    <ChevronRight
                      size={14}
                      className={`shrink-0 text-surface-500 transition-transform duration-300 ml-auto ${
                        isExpanded ? "rotate-90" : ""
                      }`}
                    />
                  </div>
                </div>

                {/* ═══ Expandable content (shared desktop + mobile) ════════ */}
                <div
                  className={`grid transition-all duration-300 ${
                    isExpanded ? "grid-rows-[1fr]" : "grid-rows-[0fr]"
                  }`}
                >
                  <div className="overflow-hidden">
                    <div className="pl-10 lg:pl-[200px] pr-4 pb-5 pt-1">
                      <div className="border-l-2 border-surface-700 pl-4">
                        <div className="font-mono text-[12px] leading-relaxed text-surface-500 space-y-0.5">
                          <div>
                            commit{" "}
                            <span className="text-surface-400 font-medium">
                              {item.hash}
                            </span>
                          </div>
                          <div>
                            Author:{" "}
                            <span className="text-surface-400">
                              OpenZync Team
                            </span>
                          </div>
                          <div>
                            Date:{" "}
                            <span className="text-accent-400">
                              {item.date}
                            </span>
                          </div>

                          <div className="border-t border-surface-800 my-2" />

                          <div className="flex items-start gap-1.5">
                            <span className="text-brand-500/60 font-semibold shrink-0 mt-0.5">
                              feat
                            </span>
                            <span className="text-surface-600 shrink-0 mt-0.5">
                              (
                            </span>
                            <span className="text-accent-400 shrink-0 mt-0.5">
                              {item.scope}
                            </span>
                            <span className="text-surface-600 shrink-0 mt-0.5">
                              ):
                            </span>
                          </div>
                          <div className="text-text-primary font-semibold ml-5 border-l-2 border-surface-700 pl-3 mt-0.5">
                            {item.title}
                          </div>
                        </div>

                        <p className="text-sm text-surface-400 leading-relaxed mt-3 font-sans">
                          {item.description}
                        </p>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}
