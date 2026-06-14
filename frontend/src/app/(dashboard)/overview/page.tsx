"use client";
import { RequireAuth } from "../require-auth";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import {
  Users,
  MessageSquare,
  MessageCircle,
  Key,
  TrendingUp,
  TrendingDown,
} from "lucide-react";

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

function timeAgo(dateStr: string): string {
  const seconds = Math.floor((Date.now() - new Date(dateStr).getTime()) / 1000);
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const mins = Math.floor(seconds / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

function actionLabel(action: string): string {
  const map: Record<string, string> = {
    "session.create": "Session created",
    "session.delete": "Session deleted",
    "user.create": "User created",
    "user.delete": "User deleted",
    "memory.ingest": "Memory ingested",
    "api_key.create": "API key created",
    "api_key.revoke": "API key revoked",
  };
  return map[action] ?? action.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function actorLabel(entry: AuditEntry): string {
  if (entry.actor_type === "api_key") return "API";
  if (entry.actor_id) return entry.actor_id.slice(0, 8);
  return "System";
}

// ─── Stat Card ─────────────────────────────────────────────────────────────────

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
        const token = sessionStorage.getItem("mg_access_token");
        const headers: Record<string, string> = {};
        if (token) headers["Authorization"] = `Bearer ${token}`;

        const [statsRes, auditRes] = await Promise.all([
          fetch("http://localhost:8000/v1/admin/stats/org", { headers }),
          fetch("http://localhost:8000/v1/admin/audit-logs?limit=5", { headers }),
        ]);

        if (statsRes.ok) setStats(await statsRes.json());
        if (auditRes.ok) {
          const auditData = await auditRes.json();
          setActivities(auditData.items ?? []);
        }
      } catch {
        // Silently fail
      } finally {
        setLoading(false);
      }
    }
    fetchData();
  }, []);

  return (
    <RequireAuth>
    <div className="space-y-6">
      {/* Page header */}
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Overview</h1>
        <p className="text-sm text-surface-400 mt-1">Summary of your organization</p>
      </div>

      {/* Stat cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {STAT_CARDS.map((card) => (
          <div key={card.key} className="stat-card">
            <div className="flex items-center gap-3">
              <div className="flex h-12 w-12 items-center justify-center rounded-full bg-brand-500/10">
                <card.icon size={22} className={card.color} />
              </div>
              <div>
                <div className="text-xs text-surface-400">{card.label}</div>
                {loading ? (
                  <div className="h-6 w-16 mt-1 rounded bg-surface-800 animate-pulse" />
                ) : (
                  <div className="text-xl font-semibold mt-0.5">
                    {stats?.[card.key]?.toLocaleString() ?? "—"}
                  </div>
                )}
              </div>
            </div>
          </div>
        ))}
      </div>

      {/* Quick actions */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="card-base p-4">
          <h3 className="text-sm font-medium mb-2">Quick Actions</h3>
          <div className="space-y-2">
            <button
              onClick={() => router.push("/memory")}
              className="btn-secondary w-full text-xs justify-start"
            >
              <MessageCircle size={14} className="mr-2" />
              Ingest Memory
            </button>
            <button
              onClick={() => router.push("/users")}
              className="btn-secondary w-full text-xs justify-start"
            >
              <Users size={14} className="mr-2" />
              Create User
            </button>
            <button
              onClick={() => router.push("/sessions")}
              className="btn-secondary w-full text-xs justify-start"
            >
              <MessageSquare size={14} className="mr-2" />
              New Session
            </button>
          </div>
        </div>

        <div className="card-base p-4 md:col-span-2">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-medium">Recent Activity</h3>
            <button className="text-xs text-accent-300 hover:text-accent-200">View all →</button>
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
          <button onClick={() => router.push("/analytics")} className="text-xs text-accent-300 hover:text-accent-200">
            View full analytics →
          </button>
        </div>
        {loading ? (
          <div className="h-12 rounded bg-surface-800 animate-pulse" />
        ) : (
          <div className="text-sm text-surface-400">
            <span className="text-[#F2F2F2] font-medium">{stats?.total_messages?.toLocaleString() ?? 0}</span> total messages ·{" "}
            <span className="text-[#F2F2F2] font-medium">{stats?.total_sessions?.toLocaleString() ?? 0}</span> sessions ·{" "}
            <span className="text-[#F2F2F2] font-medium">{stats?.total_episodes?.toLocaleString() ?? 0}</span> episodes
          </div>
        )}
      </div>
    </div>
  </RequireAuth>
  );
}
