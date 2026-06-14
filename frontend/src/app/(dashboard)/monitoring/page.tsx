"use client";

import { useEffect, useState, useCallback } from "react";
import {
  Activity,
  Timer,
  AlertTriangle,
  RefreshCw,
  Info,
  TrendingUp,
  TrendingDown,
  Database,
  Users,
} from "lucide-react";
import { cn } from "@/lib/utils";

// ─── Types ─────────────────────────────────────────────────────────────────────

interface EpisodesMetrics {
  added_total: number;
  added_24h: number;
  in_progress: number;
  enrichment_pending: number;
}

interface GraphsMetrics {
  entities_total: number;
  entities_24h: number;
  relationships_total: number;
}

interface LatencyMetrics {
  p50: number;
  p95: number;
  p99: number;
}

interface RequestRate {
  "2xx": number;
  "5xx": number;
}

interface QueueDepth {
  high: number;
  low: number;
}

interface SummaryResponse {
  episodes: EpisodesMetrics;
  graphs: GraphsMetrics;
  users_total: number;
  request_rate: RequestRate;
  error_rate_pct: number;
  overall_latency_ms: LatencyMetrics;
  context_latency_ms: LatencyMetrics;
  graph_search_latency_ms: LatencyMetrics;
  queue_depth: QueueDepth;
  active_requests: number;
  status: string;
}

interface ScrapeTarget {
  job: string;
  instance: string;
  health: string;
  last_scrape: string;
  last_error: string;
}

interface TargetsResponse {
  targets: ScrapeTarget[];
}

// ─── Constants ─────────────────────────────────────────────────────────────────

const API_BASE = "http://localhost:8000";
const REFRESH_INTERVAL_MS = 30000;

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

function formatMs(ms: number): string {
  if (ms < 0.5) return "<1ms";
  if (ms < 1) return `${(ms).toFixed(1)}ms`;
  return `${Math.round(ms)}ms`;
}

/** Color class for latency values: green <100ms, orange 100-500ms, red >500ms */
function latencyColor(ms: number): string {
  if (ms < 100) return "text-success";
  if (ms < 500) return "text-warning";
  return "text-error";
}

/** Background class for latency indicator dot */
function latencyDot(ms: number): string {
  if (ms < 100) return "bg-success";
  if (ms < 500) return "bg-warning";
  return "bg-error";
}

// ─── Sub-components ────────────────────────────────────────────────────────────

/** Skeleton placeholder matching the height of a section */
function Skeleton({ className, count = 1 }: { className?: string; count?: number }) {
  return (
    <>
      {Array.from({ length: count }, (_, i) => (
        <div key={i} className={cn("rounded bg-surface-800 animate-pulse", className)} />
      ))}
    </>
  );
}

/** Small loading spinner used during refresh */
function Spinner({ className }: { className?: string }) {
  return (
    <svg
      className={cn("animate-spin h-4 w-4", className)}
      xmlns="http://www.w3.org/2000/svg"
      fill="none"
      viewBox="0 0 24 24"
    >
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path
        className="opacity-75"
        fill="currentColor"
        d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
      />
    </svg>
  );
}

/** KPI stat card */
function KpiCard({
  label,
  value,
  icon: Icon,
  color,
  loading,
  trend,
}: {
  label: string;
  value: string | number | null | undefined;
  icon: React.ComponentType<{ size?: number; className?: string }>;
  color: string;
  loading: boolean;
  trend?: "up" | "down" | null;
}) {
  return (
    <div className="stat-card">
      <div className="flex items-center gap-3">
        <div className="flex h-12 w-12 items-center justify-center rounded-full bg-brand-500/10">
          <Icon size={22} className={color} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="text-xs text-surface-400 truncate">{label}</div>
          {loading ? (
            <div className="h-6 w-16 mt-1.5 rounded bg-surface-800 animate-pulse" />
          ) : (
            <div className="flex items-center gap-1.5 mt-0.5">
              <span className="text-xl font-semibold">
                {value != null ? value : "—"}
              </span>
              {trend === "up" && <TrendingUp size={14} className="text-success shrink-0" />}
              {trend === "down" && <TrendingDown size={14} className="text-success shrink-0" />}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/** Latency sub-card showing p50/p95/p99 with color-coded dots */
function LatencyCard({
  title,
  icon: Icon,
  data,
}: {
  title: string;
  icon: React.ComponentType<{ size?: number }>;
  data: LatencyMetrics;
}) {
  const percentiles = [
    { key: "p50" as const, label: "p50" },
    { key: "p95" as const, label: "p95" },
    { key: "p99" as const, label: "p99" },
  ];

  return (
    <div className="rounded-lg border border-surface-800 p-4 space-y-3 hover:border-surface-700 transition-colors">
      <div className="flex items-center gap-2 text-xs font-medium text-surface-400">
        <Icon size={14} />
        {title}
      </div>
      <div className="space-y-1.5">
        {percentiles.map(({ key, label }) => {
          const val = data[key];
          return (
            <div key={key} className="flex items-center justify-between text-sm">
              <span className="text-surface-500 uppercase text-[11px] font-mono tracking-wider">
                {label}
              </span>
              <div className="flex items-center gap-2">
                <span className={cn("font-mono font-medium", latencyColor(val))}>
                  {formatMs(val)}
                </span>
                <span className={cn("h-2 w-2 rounded-full", latencyDot(val))} />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ─── Page ──────────────────────────────────────────────────────────────────────

export default function MonitoringPage() {
  const [summary, setSummary] = useState<SummaryResponse | null>(null);
  const [targets, setTargets] = useState<ScrapeTarget[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null);

  // ── Data fetching ─────────────────────────────────────────────────────────

  const fetchData = useCallback(async (initial: boolean) => {
    if (initial) {
      setLoading(true);
    } else {
      setRefreshing(true);
    }

    try {
      const [summaryRes, targetsRes] = await Promise.all([
        fetch(`${API_BASE}/metrics/summary`),
        fetch(`${API_BASE}/metrics/targets`),
      ]);

      if (summaryRes.ok) {
        setSummary(await summaryRes.json() as SummaryResponse);
      }
      // ⚠️ If summary fails, we keep the existing data — silent degredation

      if (targetsRes.ok) {
        const data = await targetsRes.json() as TargetsResponse;
        setTargets(Array.isArray(data.targets) ? data.targets : []);
      }
      // ⚠️ If targets fail, keep existing data

      setLastRefreshed(new Date());
    } catch {
      // Silent fail — keep existing data on network errors
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  // Initial load + auto-refresh
  useEffect(() => {
    fetchData(true);

    const interval = setInterval(() => {
      fetchData(false);
    }, REFRESH_INTERVAL_MS);

    return () => clearInterval(interval);
  }, [fetchData]);

  // ── Computed values ──────────────────────────────────────────────────────

  const queueTotal = summary
    ? summary.queue_depth.high + summary.queue_depth.low
    : null;

  // ── Render ──────────────────────────────────────────────────────────────

  return (
    <div className="space-y-6 animate-fade-in">
      {/* ═══════════════════════════════════════════════════════════════════
          Page header
      ════════════════════════════════════════════════════════════════════ */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
            <Activity size={24} className="text-brand-300" />
            Monitoring
          </h1>
          <p className="text-sm text-surface-400 mt-1">
            Real-time platform performance and health metrics
          </p>
        </div>

        <div className="flex items-center gap-3">
          {/* Live badge */}
          <span className="flex items-center gap-1.5 text-xs text-success font-medium">
            <span className="h-2 w-2 rounded-full bg-success animate-pulse-dot" />
            Live
          </span>

          {/* Last refreshed */}
          {lastRefreshed && (
            <span className="text-[11px] text-surface-500 hidden sm:block">
              Updated {timeAgo(lastRefreshed.toISOString())}
            </span>
          )}

          {/* Refresh button */}
          <button
            onClick={() => fetchData(false)}
            disabled={refreshing}
            className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-white disabled:opacity-50"
            title="Refresh now"
          >
            {refreshing ? (
              <Spinner />
            ) : (
              <RefreshCw size={16} />
            )}
          </button>
        </div>
      </div>

      {/* ═══════════════════════════════════════════════════════════════════
          KPI cards row
      ════════════════════════════════════════════════════════════════════ */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <KpiCard
          label="Episodes Added (24h)"
          value={summary?.episodes.added_24h?.toLocaleString() ?? null}
          icon={TrendingUp}
          color="text-brand-300"
          loading={loading}
          trend={summary && summary.episodes.added_24h > 0 ? "up" : null}
        />
        <KpiCard
          label="Enrichment Pending"
          value={summary?.episodes.enrichment_pending?.toLocaleString() ?? null}
          icon={Timer}
          color="text-warning"
          loading={loading}
          trend={
            summary && summary.episodes.enrichment_pending > 0 ? "up" : null
          }
        />
        <KpiCard
          label="Error Rate"
          value={
            summary != null
              ? `${summary.error_rate_pct.toFixed(2)}%`
              : null
          }
          icon={AlertTriangle}
          color={
            summary && summary.error_rate_pct > 5
              ? "text-error"
              : summary && summary.error_rate_pct > 1
                ? "text-warning"
                : "text-success"
          }
          loading={loading}
          trend={
            summary && summary.error_rate_pct > 1 ? "up" : "down"
          }
        />
        <KpiCard
          label="Queue Depth"
          value={queueTotal?.toLocaleString() ?? null}
          icon={Activity}
          color={
            queueTotal != null && queueTotal > 100
              ? "text-warning"
              : queueTotal != null && queueTotal > 500
                ? "text-error"
                : "text-surface-300"
          }
          loading={loading}
          trend={
            summary && summary.queue_depth.high > 0 ? "up" : null
          }
        />
      </div>

      {/* ═══════════════════════════════════════════════════════════════════
          Latency panel
      ════════════════════════════════════════════════════════════════════ */}
      <div className="card-base p-5">
        <h3 className="text-sm font-medium flex items-center gap-1.5 mb-4">
          <Timer size={16} className="text-brand-300" />
          Latency
        </h3>

        {loading ? (
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {[1, 2, 3].map((i) => (
              <div key={i} className="h-28 rounded-lg bg-surface-800 animate-pulse" />
            ))}
          </div>
        ) : summary ? (
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <LatencyCard
              title="Overall API"
              icon={Activity}
              data={summary.overall_latency_ms}
            />
            <LatencyCard
              title="Context Assembly"
              icon={Timer}
              data={summary.context_latency_ms}
            />
            <LatencyCard
              title="Graph Search"
              icon={Database}
              data={summary.graph_search_latency_ms}
            />
          </div>
        ) : (
          <div className="flex flex-col items-center justify-center h-28 text-surface-500">
            <Timer size={28} className="mb-2 opacity-40" />
            <p className="text-sm">No latency data available</p>
          </div>
        )}
      </div>

      {/* ═══════════════════════════════════════════════════════════════════
          Scrape Targets table
      ════════════════════════════════════════════════════════════════════ */}
      <div className="card-base overflow-hidden">
        <div className="px-5 py-4 border-b border-surface-800 flex items-center justify-between">
          <h3 className="text-sm font-medium flex items-center gap-1.5">
            <Info size={16} className="text-brand-300" />
            Scrape Targets
          </h3>
          {!loading && (
            <span className="text-[11px] text-surface-500">
              {targets.length} target{targets.length !== 1 ? "s" : ""}
            </span>
          )}
        </div>

        {loading ? (
          <div className="p-5 space-y-3">
            {[1, 2, 3, 4].map((i) => (
              <div key={i} className="flex gap-4">
                <div className="h-5 w-32 rounded bg-surface-800 animate-pulse" />
                <div className="h-5 w-48 rounded bg-surface-800 animate-pulse" />
                <div className="h-5 w-16 rounded bg-surface-800 animate-pulse" />
                <div className="h-5 w-20 rounded bg-surface-800 animate-pulse" />
                <div className="h-5 w-40 rounded bg-surface-800 animate-pulse flex-1" />
              </div>
            ))}
          </div>
        ) : targets.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 text-surface-500">
            <Info size={36} className="mb-3 opacity-40" />
            <p className="text-sm font-medium">No scrape targets found</p>
            <p className="text-xs mt-1 text-surface-600">
              Targets will appear once Prometheus scrape jobs are configured.
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-surface-800">
                  <th className="px-5 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">
                    Job
                  </th>
                  <th className="px-5 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">
                    Instance
                  </th>
                  <th className="px-5 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">
                    Health
                  </th>
                  <th className="px-5 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">
                    Last Scrape
                  </th>
                  <th className="px-5 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">
                    Last Error
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-surface-800">
                {targets.map((t, i) => {
                  const isUp = t.health?.toLowerCase() === "up";
                  return (
                    <tr
                      key={`${t.job}-${t.instance}-${i}`}
                      className={cn(
                        "transition-colors hover:bg-surface-800/50",
                        i % 2 === 0 ? "bg-surface-950/50" : "",
                      )}
                    >
                      {/* Job */}
                      <td className="px-5 py-3">
                        <span className="font-mono text-xs text-surface-200">
                          {t.job}
                        </span>
                      </td>

                      {/* Instance */}
                      <td className="px-5 py-3">
                        <span className="font-mono text-xs text-surface-300">
                          {t.instance}
                        </span>
                      </td>

                      {/* Health chip */}
                      <td className="px-5 py-3">
                        <span
                          className={cn(
                            "inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium tracking-wide",
                            isUp
                              ? "bg-success/10 text-success"
                              : "bg-error/10 text-error",
                          )}
                        >
                          {isUp ? "UP" : "DOWN"}
                        </span>
                      </td>

                      {/* Last Scrape */}
                      <td className="px-5 py-3">
                        <span className="text-xs text-surface-400">
                          {t.last_scrape ? timeAgo(t.last_scrape) : "—"}
                        </span>
                      </td>

                      {/* Last Error */}
                      <td className="px-5 py-3">
                        {t.last_error ? (
                          <span
                            className="text-xs text-surface-500 max-w-[220px] block truncate"
                            title={t.last_error}
                          >
                            {t.last_error}
                          </span>
                        ) : (
                          <span className="text-xs text-surface-600">—</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ═══════════════════════════════════════════════════════════════════
          Status bar
      ════════════════════════════════════════════════════════════════════ */}
      <div
        className={cn(
          "card-base px-5 py-3 flex items-center justify-between flex-wrap gap-2",
          !summary && "opacity-50",
        )}
      >
        {summary ? (
          <>
            <div className="flex items-center gap-4 text-xs text-surface-400">
              <span>
                Status:{" "}
                <span
                  className={cn(
                    "font-medium capitalize",
                    summary.status === "healthy" || summary.status === "up"
                      ? "text-success"
                      : "text-warning",
                  )}
                >
                  {summary.status}
                </span>
              </span>
              <span className="hidden sm:inline">
                Active Requests:{" "}
                <span className="text-surface-200 font-medium">
                  {summary.active_requests.toLocaleString()}
                </span>
              </span>
              <span className="hidden sm:inline">
                Users:{" "}
                <span className="text-surface-200 font-medium">
                  {summary.users_total?.toLocaleString() ?? "—"}
                </span>
              </span>
              <span>
                Request Rate:{" "}
                <span className="text-surface-200 font-medium">
                  {summary.request_rate["2xx"].toLocaleString()} 2xx
                </span>
                {" / "}
                <span
                  className={cn(
                    "font-medium",
                    summary.request_rate["5xx"] > 0 ? "text-error" : "text-surface-200",
                  )}
                >
                  {summary.request_rate["5xx"].toLocaleString()} 5xx
                </span>
              </span>
            </div>
            <div className="text-[11px] text-surface-500">
              {refreshing ? (
                <span className="flex items-center gap-1.5">
                  <Spinner />
                  Refreshing...
                </span>
              ) : (
                `Auto-refreshes every ${REFRESH_INTERVAL_MS / 1000}s`
              )}
            </div>
          </>
        ) : loading ? (
          <div className="flex gap-4">
            <Skeleton className="h-4 w-20" count={3} />
          </div>
        ) : (
          <div className="flex items-center gap-2 text-xs text-surface-500">
            <AlertTriangle size={14} />
            Unable to fetch monitoring data
          </div>
        )}
      </div>
    </div>
  );
}
