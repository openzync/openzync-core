"use client";
import { RequireAuth } from "../require-auth";

import { useEffect, useState, useCallback, useRef } from "react";
import {
  Shield,
  RefreshCw,
  ChevronLeft,
  ChevronRight,
  X,
  Search,
} from "lucide-react";
import { cn } from "@/lib/utils";

// ─── Types ─────────────────────────────────────────────────────────────────────

interface AuditEntry {
  id: string;
  action: string;
  actor_id: string | null;
  actor_type: string | null;
  resource_type: string | null;
  resource_id: string | null;
  details: Record<string, unknown> | null;
  ip_address: string | null;
  status_code: number | null;
  method: string | null;
  path: string | null;
  created_at: string;
}

interface AuditResponse {
  items: AuditEntry[];
  total: number;
}

// ─── Constants ─────────────────────────────────────────────────────────────────

const API_BASE = "http://localhost:8000";
const PAGE_SIZE = 25;

// ─── Helpers ───────────────────────────────────────────────────────────────────

function authHeaders(): Record<string, string> {
  const token = sessionStorage.getItem("mg_access_token");
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  return headers;
}

function formatTime(dateStr: string): string {
  const d = new Date(dateStr);
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  const diffMins = Math.floor(diffMs / 60_000);

  if (diffMins < 1) return "just now";
  if (diffMins < 60) return `${diffMins}m ago`;

  const timeStr = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const dateStr_fmt = d.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
  });

  // If today, show time only
  if (d.toDateString() === now.toDateString()) return timeStr;
  // If this year, show month day time
  if (d.getFullYear() === now.getFullYear()) return `${dateStr_fmt} ${timeStr}`;
  return `${dateStr_fmt} ${d.getFullYear()} ${timeStr}`;
}

function statusColor(code: number | null): string {
  if (code === null) return "bg-surface-700 text-surface-400";
  if (code < 300) return "bg-success/10 text-success";
  if (code < 500) return "bg-warning/10 text-warning";
  return "bg-error/10 text-error";
}

function actorTypeLabel(type: string | null): string {
  if (!type) return "system";
  const map: Record<string, string> = {
    user: "User",
    api_key: "API Key",
    system: "System",
  };
  return map[type] ?? type;
}

function actorTypeColor(type: string | null): string {
  if (!type || type === "system") return "bg-surface-700 text-surface-300";
  if (type === "user") return "bg-brand-500/10 text-brand-300";
  if (type === "api_key") return "bg-accent-300/10 text-accent-300";
  return "bg-surface-700 text-surface-300";
}

// ─── Main Page ─────────────────────────────────────────────────────────────────

export default function AuditLogPage() {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Filters
  const [filterAction, setFilterAction] = useState("");
  const [filterActorType, setFilterActorType] = useState("all");
  const [filterStatus, setFilterStatus] = useState("all");

  // Pagination
  const [offset, setOffset] = useState(0);
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  // Auto-refresh
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<string | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // ── Fetch ──────────────────────────────────────────────────────────────────

  const fetchLogs = useCallback(async (currentOffset: number) => {
    setLoading(true);
    setError(null);

    try {
      const params = new URLSearchParams({
        limit: String(PAGE_SIZE),
        offset: String(currentOffset),
      });
      if (filterAction.trim()) params.set("action", filterAction.trim());
      if (filterActorType !== "all") params.set("actor_type", filterActorType);
      if (filterStatus !== "all") {
        // Map user-friendly status filters to API params
        if (filterStatus === "2xx") params.set("status_code", "2");
        else if (filterStatus === "4xx") params.set("status_code", "4");
        else if (filterStatus === "5xx") params.set("status_code", "5");
      }

      const res = await fetch(`${API_BASE}/v1/admin/audit-logs?${params}`, {
        headers: authHeaders(),
      });

      if (!res.ok) throw new Error("Failed to load audit logs");

      const data: AuditResponse = await res.json();
      setEntries(data.items ?? []);
      setTotal(data.total ?? 0);
      setLastUpdated(new Date().toLocaleTimeString());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load audit logs");
    } finally {
      setLoading(false);
    }
  }, [filterAction, filterActorType, filterStatus]);

  // Initial fetch and on filter/pagination change
  useEffect(() => {
    fetchLogs(offset);
  }, [offset, fetchLogs]);

  // Auto-refresh
  useEffect(() => {
    if (autoRefresh) {
      intervalRef.current = setInterval(() => {
        fetchLogs(offset);
      }, 10000); // Refresh every 10s
    }
    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, [autoRefresh, offset, fetchLogs]);

  // ── Filter handlers ────────────────────────────────────────────────────────

  const applyFilters = () => {
    setOffset(0);
  };

  const clearFilters = () => {
    setFilterAction("");
    setFilterActorType("all");
    setFilterStatus("all");
    setOffset(0);
  };

  const hasActiveFilters = filterAction.trim() || filterActorType !== "all" || filterStatus !== "all";

  // ── Pagination ─────────────────────────────────────────────────────────────

  const goToPrevious = () => {
    setOffset((prev) => Math.max(0, prev - PAGE_SIZE));
  };

  const goToNext = () => {
    setOffset((prev) => {
      const next = prev + PAGE_SIZE;
      return next < total ? next : prev;
    });
  };

  // ── Skeleton rows ──────────────────────────────────────────────────────────

  const skeletonRows = Array.from({ length: 6 }, (_, i) => (
    <tr key={`skel-${i}`} className={i % 2 === 0 ? "bg-surface-950/50" : ""}>
      {[1, 2, 3, 4, 5, 6, 7].map((col) => (
        <td key={col} className="px-4 py-3">
          <div className="h-4 rounded bg-surface-800 animate-pulse" style={{ width: col === 2 ? "120px" : "70px" }} />
        </td>
      ))}
    </tr>
  ));

  // ── Empty state ────────────────────────────────────────────────────────────

  const emptyRow = (
    <tr>
      <td colSpan={8}>
        <div className="flex flex-col items-center justify-center py-16 text-surface-500">
          <Shield size={40} className="mb-3 opacity-40" />
          <p className="text-sm font-medium">No audit entries found</p>
          <p className="text-xs mt-1">
            {hasActiveFilters ? "Try adjusting your filters" : "Audit entries will appear here as actions are performed"}
          </p>
        </div>
      </td>
    </tr>
  );

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
    <RequireAuth>
    <div className="space-y-6">
      {/* Page header */}
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Audit Log</h1>
        <p className="text-sm text-surface-400 mt-1">Immutable record of all system actions</p>
      </div>

      {/* Filter bar */}
      <div className="card-base p-3">
        <div className="flex flex-wrap items-end gap-3">
          {/* Action filter */}
          <div className="flex-1 min-w-[160px]">
            <label className="block text-xs font-medium text-surface-400 mb-1">
              Action
            </label>
            <div className="relative">
              <input
                className="input-base pl-8 text-sm"
                placeholder="e.g. session.create"
                value={filterAction}
                onChange={(e) => setFilterAction(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && applyFilters()}
              />
              <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-surface-500 pointer-events-none" />
            </div>
          </div>

          {/* Actor Type filter */}
          <div className="w-36">
            <label className="block text-xs font-medium text-surface-400 mb-1">
              Actor Type
            </label>
            <select
              className="input-base appearance-none cursor-pointer text-sm"
              value={filterActorType}
              onChange={(e) => setFilterActorType(e.target.value)}
            >
              <option value="all">All</option>
              <option value="user">User</option>
              <option value="api_key">API Key</option>
              <option value="system">System</option>
            </select>
          </div>

          {/* Status filter */}
          <div className="w-28">
            <label className="block text-xs font-medium text-surface-400 mb-1">
              Status
            </label>
            <select
              className="input-base appearance-none cursor-pointer text-sm"
              value={filterStatus}
              onChange={(e) => setFilterStatus(e.target.value)}
            >
              <option value="all">All</option>
              <option value="2xx">2xx</option>
              <option value="4xx">4xx</option>
              <option value="5xx">5xx</option>
            </select>
          </div>

          {/* Apply / Clear */}
          <div className="flex items-center gap-2 pb-0.5">
            <button onClick={applyFilters} className="btn-primary text-xs">
              Apply
            </button>
            {hasActiveFilters && (
              <button onClick={clearFilters} className="btn-ghost text-xs text-surface-400">
                <X size={14} />
                Clear
              </button>
            )}
          </div>

          {/* Spacer */}
          <div className="flex-1" />

          {/* Auto-refresh toggle */}
          <div className="flex items-center gap-2 pb-0.5">
            <label className="flex items-center gap-2 cursor-pointer">
              <div
                onClick={() => setAutoRefresh((prev) => !prev)}
                className={cn(
                  "relative inline-flex h-5 w-9 rounded-full transition-colors",
                  autoRefresh ? "bg-brand-500" : "bg-surface-700",
                )}
              >
                <span
                  className={cn(
                    "inline-block h-4 w-4 rounded-full bg-white transition-transform mt-0.5 ml-0.5",
                    autoRefresh ? "translate-x-4" : "translate-x-0",
                  )}
                />
              </div>
              <span className="text-xs text-surface-400">Auto-refresh</span>
            </label>
            {lastUpdated && (
              <span className="text-[11px] text-surface-500">
                Updated {lastUpdated}
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Table */}
      <div className="card-base overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-surface-800">
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Time</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Action</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Actor</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Type</th>
                <th className="px-4 py-3 text-center text-xs font-medium uppercase tracking-wider text-surface-400">Status</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Method</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Path</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">IP</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-surface-800">
              {loading
                ? skeletonRows
                : entries.length === 0
                  ? emptyRow
                  : entries.map((entry, idx) => (
                      <tr
                        key={entry.id}
                        className={cn(
                          "transition-colors hover:bg-surface-800/50",
                          idx % 2 === 0 ? "bg-surface-950/50" : "",
                        )}
                      >
                        {/* Time */}
                        <td className="px-4 py-3 whitespace-nowrap">
                          <span className="text-surface-300 text-xs">{formatTime(entry.created_at)}</span>
                        </td>

                        {/* Action — monospace */}
                        <td className="px-4 py-3">
                          <span className="font-mono text-xs text-surface-200">{entry.action}</span>
                        </td>

                        {/* Actor — truncated ID */}
                        <td className="px-4 py-3">
                          <span
                            className="font-mono text-xs text-surface-400 max-w-[80px] block truncate"
                            title={entry.actor_id ?? undefined}
                          >
                            {entry.actor_id ? entry.actor_id.slice(0, 12) : "—"}
                          </span>
                        </td>

                        {/* Actor Type chip */}
                        <td className="px-4 py-3">
                          <span
                            className={cn(
                              "inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium",
                              actorTypeColor(entry.actor_type),
                            )}
                          >
                            {actorTypeLabel(entry.actor_type)}
                          </span>
                        </td>

                        {/* Status code — colored chip */}
                        <td className="px-4 py-3 text-center">
                          {entry.status_code !== null ? (
                            <span
                              className={cn(
                                "inline-flex items-center justify-center rounded px-1.5 py-0.5 text-xs font-mono font-medium min-w-[32px]",
                                statusColor(entry.status_code),
                              )}
                            >
                              {entry.status_code}
                            </span>
                          ) : (
                            <span className="text-surface-600 text-xs">—</span>
                          )}
                        </td>

                        {/* Method */}
                        <td className="px-4 py-3">
                          {entry.method ? (
                            <span className="text-xs font-mono text-surface-400">{entry.method}</span>
                          ) : (
                            <span className="text-surface-600 text-xs">—</span>
                          )}
                        </td>

                        {/* Path — truncated */}
                        <td className="px-4 py-3">
                          {entry.path ? (
                            <span
                              className="text-xs text-surface-400 max-w-[140px] block truncate font-mono"
                              title={entry.path}
                            >
                              {entry.path}
                            </span>
                          ) : (
                            <span className="text-surface-600 text-xs">—</span>
                          )}
                        </td>

                        {/* IP */}
                        <td className="px-4 py-3">
                          <span className="text-xs font-mono text-surface-500">
                            {entry.ip_address ?? "—"}
                          </span>
                        </td>
                      </tr>
                    ))}
            </tbody>
          </table>
        </div>

        {/* Error state */}
        {error && !loading && (
          <div className="border-t border-surface-800 px-4 py-3">
            <div className="rounded-md bg-error/10 border border-error/30 px-3 py-2 text-sm text-error flex items-center gap-2">
              <X size={14} />
              {error}
              <button
                onClick={() => fetchLogs(offset)}
                className="ml-auto btn-ghost text-xs text-error hover:text-white"
              >
                Retry
              </button>
            </div>
          </div>
        )}

        {/* Pagination footer */}
        {!loading && total > 0 && (
          <div className="border-t border-surface-800 px-4 py-3 flex items-center justify-between">
            <span className="text-xs text-surface-500">
              {total} total entr{total === 1 ? "y" : "ies"}
            </span>
            <div className="flex items-center gap-3">
              <span className="text-xs text-surface-400">
                Page {currentPage} of {totalPages}
              </span>
              <div className="flex items-center gap-1">
                <button
                  onClick={goToPrevious}
                  disabled={offset === 0}
                  className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-white disabled:opacity-30 disabled:cursor-not-allowed"
                  title="Previous page"
                >
                  <ChevronLeft size={14} />
                </button>
                <button
                  onClick={goToNext}
                  disabled={offset + PAGE_SIZE >= total}
                  className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-white disabled:opacity-30 disabled:cursor-not-allowed"
                  title="Next page"
                >
                  <ChevronRight size={14} />
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Auto-refresh indicator */}
        {autoRefresh && !loading && (
          <div className="border-t border-surface-800 px-4 py-2 flex items-center gap-2">
            <RefreshCw size={12} className="text-brand-300 animate-spin-slow" />
            <span className="text-[11px] text-surface-500">Auto-refreshing every 10s</span>
          </div>
        )}
      </div>
    </div>
  </RequireAuth>
  );
}
