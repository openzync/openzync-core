"use client";

import { useState } from "react";
import { cn } from "@/lib/utils";
import { audienceTabs } from "@/content/audience-tabs";
import { ArrowRight } from "lucide-react";
import Link from "next/link";

/**
 * Tabbed audience section — Plone's "What Plone Can Do for You" pattern.
 * Three audience tabs (Developers, Tech Leads, Product Managers) with
 * content panels containing descriptive text + bullet links.
 */
export function AudienceTabs() {
  const [activeTab, setActiveTab] = useState(audienceTabs[0]?.key ?? "");

  const currentTab = audienceTabs.find((t) => t.key === activeTab);
  if (!currentTab) return null;

  return (
    <section className="py-20 md:py-28 border-t border-surface-800">
      <div className="mx-auto max-w-7xl px-6">
        <div className="max-w-4xl mx-auto">
          <h2 className="text-3xl md:text-4xl font-bold tracking-tight text-center mb-2">
            OpenZep for
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
                    ? "bg-brand-500 text-white shadow-lg shadow-brand-500/20"
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
            <ul className="space-y-3">
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
          </div>
        </div>
      </div>
    </section>
  );
}
