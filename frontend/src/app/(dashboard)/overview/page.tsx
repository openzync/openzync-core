"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import {
  Users,
  MessageSquare,
  MessageCircle,
  Key,
} from "lucide-react";

import { get } from "@/lib/api-client";
import { timeAgo, actionLabel, formatNumber } from "@/lib/utils";
import { PageHeader } from "@/components/shared/page-header";
import { StatCard } from "@/components/shared/stat-card";
import { Button } from "@/components/ui/button";

// ─── Types ─────────────────────────────────────────────────────────────────────

interface OrgStats {
  total_users: number;
  total_sessions: number;
  total_messages: number;
  total_api_keys: number;
  total_episodes: number;
  total_facts: number;
}

interface AuditEntry {
  id: string;
  action: string;
  actor_id: string | null;
  actor_type: string | null;
  created_at: string;
  status_code: number | null;
}

// ─── Helpers ───────────────────────────────────────────────────────────────────

function actorLabel(entry: AuditEntry): string {
  if (entry.actor_type === "api_key") return "API";
  if (entry.actor_id) return entry.actor_id.slice(0, 8);
  return "System";
}

// ─── Stat Card Config ──────────────────────────────────────────────────────────

const STAT_CARDS = [
  { label: "Total Users", key: "total_users" as const, icon: Users, color: "text-brand-300" },
  { label: "Total Sessions", key: "total_sessions" as const, icon: MessageSquare, color: "text-accent-300" },
  { label: "Total Messages", key: "total_messages" as const, icon: MessageCircle, color: "text-success" },
  { label: "API Keys", key: "total_api_keys" as const, icon: Key, color: "text-surface-300" },
];

// ─── Page ──────────────────────────────────────────────────────────────────────

export default function OverviewPage() {
  const router = useRouter();
  const [stats, setStats] = useState<OrgStats | null>(null);
  const [activities, setActivities] = useState<AuditEntry[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function fetchData() {
      try {
        const [statsRes, auditRes] = await Promise.all([
          get<OrgStats>("/v1/admin/stats/org"),
          get<{ items: AuditEntry[] }>("/v1/admin/audit-logs?limit=5"),
        ]);
        setStats(statsRes);
        setActivities(auditRes.items ?? []);
      } catch {
        // Silently fail on overview — non-critical data
      } finally {
        setLoading(false);
      }
    }
    fetchData();
  }, []);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Overview"
        description="Summary of your organization"
      />

      {/* Stat cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {STAT_CARDS.map((card) => (
          <StatCard
            key={card.key}
            label={card.label}
            value={stats?.[card.key] ?? null}
            icon={card.icon}
            color={card.color}
            loading={loading}
          />
        ))}
      </div>

      {/* Quick actions + Recent Activity */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="card-base p-4">
          <h3 className="text-sm font-medium mb-2">Quick Actions</h3>
          <div className="space-y-2">
            <Button
              variant="secondary"
              size="sm"
              onClick={() => router.push("/memory")}
              className="w-full justify-start"
            >
              <MessageCircle size={14} className="mr-2" />
              Ingest Memory
            </Button>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => router.push("/users")}
              className="w-full justify-start"
            >
              <Users size={14} className="mr-2" />
              Create User
            </Button>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => router.push("/sessions")}
              className="w-full justify-start"
            >
              <MessageSquare size={14} className="mr-2" />
              New Session
            </Button>
          </div>
        </div>

        <div className="card-base p-4 md:col-span-2">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-medium">Recent Activity</h3>
            <button
              onClick={() => router.push("/audit")}
              className="text-xs text-accent-300 hover:text-accent-200"
            >
              View all →
            </button>
          </div>
          {activities.length === 0 ? (
            <div className="text-sm text-surface-500 py-4 text-center">
              {loading ? (
                <div className="space-y-3">
                  {[1, 2, 3].map((i) => (
                    <div key={i} className="h-8 rounded bg-surface-800 animate-pulse" />
                  ))}
                </div>
              ) : (
                "No recent activity found."
              )}
            </div>
          ) : (
            <div className="space-y-2">
              {activities.map((entry) => (
                <div key={entry.id} className="flex items-center gap-3 text-sm">
                  <div className="h-2 w-2 rounded-full bg-accent-300/50 shrink-0" />
                  <span className="text-surface-300 flex-1">{actionLabel(entry.action)}</span>
                  <span className="text-surface-500 text-xs">{actorLabel(entry)}</span>
                  <span className="text-surface-600 text-xs">{timeAgo(entry.created_at)}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Usage overview */}
      <div className="card-base p-4">
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-medium">Daily Usage (7 days)</h3>
          <button
            onClick={() => router.push("/analytics")}
            className="text-xs text-accent-300 hover:text-accent-200"
          >
            View full analytics →
          </button>
        </div>
        {loading ? (
          <div className="h-12 rounded bg-surface-800 animate-pulse" />
        ) : (
          <div className="text-sm text-surface-400">
            <span className="text-text-primary font-medium">{formatNumber(stats?.total_messages ?? 0)}</span> total messages ·
            <span className="text-text-primary font-medium ml-1">{formatNumber(stats?.total_sessions ?? 0)}</span> sessions ·
            <span className="text-text-primary font-medium ml-1">{formatNumber(stats?.total_episodes ?? 0)}</span> episodes
          </div>
        )}
      </div>
    </div>
  );
}
