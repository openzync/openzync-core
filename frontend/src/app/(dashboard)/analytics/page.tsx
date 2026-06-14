"use client";
import { RequireAuth } from "../require-auth";

import { useEffect, useState, useRef } from "react";
import {
  BarChart3,
  MessageSquare,
  MessageCircle,
  Database,
  TrendingUp,
  Users,
} from "lucide-react";
import { cn } from "@/lib/utils";

// ─── Types ─────────────────────────────────────────────────────────────────────

interface OrgStats {
  total_messages: number;
  total_sessions: number;
  total_facts: number;
  total_users: number;
  total_api_keys: number;
  total_episodes: number;
}

interface UsagePoint {
  date: string;
  message_count: number;
  session_count: number;
}

// ─── Constants ─────────────────────────────────────────────────────────────────

const API_BASE = "http://localhost:8000";
const DAYS_OPTIONS = [7, 30, 90] as const;
type DaysOption = (typeof DAYS_OPTIONS)[number];

const STAT_CARDS = [
  {
    label: "Total Messages",
    key: "total_messages" as const,
    icon: MessageCircle,
    color: "text-brand-300",
  },
  {
    label: "Total Sessions",
    key: "total_sessions" as const,
    icon: MessageSquare,
    color: "text-accent-300",
  },
  {
    label: "Total Facts",
    key: "total_facts" as const,
    icon: Database,
    color: "text-success",
  },
];

// ─── Helpers ───────────────────────────────────────────────────────────────────

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const token = sessionStorage.getItem("mg_access_token");
  if (token) headers["Authorization"] = `Bearer ${token}`;
  return headers;
}

/** Abbreviate an ISO date string to "Jan 1" format. */
function abbrevDate(dateStr: string): string {
  const d = new Date(dateStr);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

/** Round a number up to the nearest nice tick value (e.g., 1423 → 2000). */
function niceMax(value: number): number {
  if (value <= 0) return 100;
  const magnitude = Math.pow(10, Math.floor(Math.log10(value)));
  const normalized = value / magnitude;
  if (normalized <= 1) return magnitude;
  if (normalized <= 2) return 2 * magnitude;
  if (normalized <= 5) return 5 * magnitude;
  return 10 * magnitude;
}

// ─── Theme token helpers (read CSS custom properties) ─────────────────────────

function cssVar(name: string): string {
  if (typeof window === "undefined") return "#14488C";
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#14488C";
}

// ─── Page ──────────────────────────────────────────────────────────────────────

export default function AnalyticsPage() {
  const [stats, setStats] = useState<OrgStats | null>(null);
  const [usage, setUsage] = useState<UsagePoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [usageLoading, setUsageLoading] = useState(false);
  const [days, setDays] = useState<DaysOption>(30);

  // Chart state
  const chartRef = useRef<HTMLDivElement>(null);
  const [chartWidth, setChartWidth] = useState(0);
  const [hoveredBar, setHoveredBar] = useState<{
    index: number;
    value: number;
    sessionValue: number;
    date: string;
    x: number;
  } | null>(null);

  // ── Chart container measurement ──────────────────────────────────────────

  useEffect(() => {
    function measure() {
      if (chartRef.current) {
        setChartWidth(chartRef.current.clientWidth);
      }
    }
    // Delay measurement to ensure layout is settled
    const raf = requestAnimationFrame(measure);
    window.addEventListener("resize", measure);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", measure);
    };
  }, []);

  // ── Fetch org stats (all-time, independent of time range) ───────────────

  useEffect(() => {
    let cancelled = false;
    async function fetchStats() {
      try {
        const res = await fetch(`${API_BASE}/v1/admin/stats/org`, { headers: authHeaders() });
        if (res.ok && !cancelled) {
          setStats(await res.json());
        }
      } catch {
        // Silent fail — keep existing data
      }
    }
    fetchStats();
    return () => {
      cancelled = true;
    };
  }, []);

  // ── Fetch usage data (depends on selected days) ─────────────────────────

  useEffect(() => {
    let cancelled = false;
    setUsageLoading(true);

    async function fetchUsage() {
      try {
        const res = await fetch(`${API_BASE}/v1/admin/stats/usage?days=${days}`, { headers: authHeaders() });
        if (res.ok && !cancelled) {
          const data = await res.json();
          setUsage(Array.isArray(data) ? data : []);
        }
      } catch {
        // Silent fail — keep existing data
      } finally {
        if (!cancelled) {
          setUsageLoading(false);
          setLoading(false);
        }
      }
    }

    fetchUsage();
    return () => {
      cancelled = true;
    };
  }, [days]);

  // ═══════════════════════════════════════════════════════════════════════
  //  Chart computation
  // ═══════════════════════════════════════════════════════════════════════

  const CHART_HEIGHT = 260;
  const PADDING = { top: 20, right: 16, bottom: 48, left: 52 };

  // Effective draw area (clamped to avoid negative dimensions on tiny containers)
  const drawWidth = Math.max(chartWidth - PADDING.left - PADDING.right, 60);
  const drawHeight = CHART_HEIGHT - PADDING.top - PADDING.bottom;

  // Y-axis scale
  const rawMax = usage.length > 0
    ? Math.max(...usage.map((p) => Math.max(p.message_count, p.session_count)))
    : 0;
  const yMax = niceMax(rawMax);

  // Y-axis tick values (3 ticks for a clean scale)
  const yTicks = [0, Math.round(yMax / 2), yMax];

  // Bar geometry
  const dataCount = usage.length;
  const barSlotWidth = dataCount > 0 ? drawWidth / dataCount : 0;
  const barWidth = Math.max(Math.min(barSlotWidth * 0.55, 36), 3);
  const barGap = (barSlotWidth - barWidth) / 2;

  // Label skipping for dense data
  const maxLabelSlots = Math.floor(drawWidth / 55);
  const labelStep = dataCount > 0 ? Math.max(1, Math.ceil(dataCount / Math.max(maxLabelSlots, 1))) : 1;

  // ── Render helpers ───────────────────────────────────────────────────────

  function renderChartSkeleton() {
    return (
      <div className="flex items-end gap-1 h-[260px] pt-5">
        {Array.from({ length: 20 }, (_, i) => (
          <div
            key={i}
            className="flex-1 rounded-t bg-surface-800 animate-pulse"
            style={{
              height: `${30 + Math.random() * 70}%`,
              opacity: 0.3 + Math.random() * 0.7,
            }}
          />
        ))}
      </div>
    );
  }

  function renderEmptyState() {
    return (
      <div className="flex flex-col items-center justify-center h-[260px] text-surface-500">
        <BarChart3 size={40} className="mb-3 opacity-40" />
        <p className="text-sm font-medium">No usage data available for this period.</p>
        <p className="text-xs mt-1 text-surface-600">
          Try selecting a different time range.
        </p>
      </div>
    );
  }

  function renderChart() {
    if (chartWidth === 0) return <div className="h-[260px]" />;
    if (loading || (usageLoading && usage.length === 0)) return renderChartSkeleton();
    if (usage.length === 0) return renderEmptyState();

    return (
      <div className="relative w-full">
        <svg
          viewBox={`0 0 ${chartWidth} ${CHART_HEIGHT}`}
          className="w-full overflow-visible"
          preserveAspectRatio="xMidYMid meet"
        >
          {/* ── Y-axis grid lines + labels ─────────────────────────────── */}
          {yTicks.map((tick) => {
            const y = PADDING.top + drawHeight - (tick / yMax) * drawHeight;
            return (
              <g key={tick}>
                <line
                  x1={PADDING.left}
                  y1={y}
                  x2={PADDING.left + drawWidth}
                  y2={y}
                  stroke={cssVar("--color-surface-800") || "#1A2332"}
                  strokeWidth={1}
                />
                <text
                  x={PADDING.left - 8}
                  y={y + 4}
                  textAnchor="end"
                  fill={cssVar("--color-surface-500") || "#6C7A8E"}
                  fontSize={11}
                  fontFamily="var(--font-mono)"
                >
                  {tick >= 1000 ? `${(tick / 1000).toFixed(1)}k` : tick.toLocaleString()}
                </text>
              </g>
            );
          })}

          {/* ── Zero baseline ──────────────────────────────────────────── */}
          <line
            x1={PADDING.left}
            y1={PADDING.top + drawHeight}
            x2={PADDING.left + drawWidth}
            y2={PADDING.top + drawHeight}
            stroke={cssVar("--color-surface-600") || "#4E5A6E"}
            strokeWidth={1}
          />

          {/* ── Bars ──────────────────────────────────────────────────── */}
          {usage.map((point, i) => {
            const barX = PADDING.left + i * barSlotWidth + barGap;
            const messageHeight = (point.message_count / yMax) * drawHeight;
            const sessionHeight = (point.session_count / yMax) * drawHeight;
            const isHovered = hoveredBar?.index === i;
            const dimmed = hoveredBar !== null && !isHovered;

            return (
              <g key={point.date}>
                {/* Session bar (accent-300, behind message bar) */}
                <rect
                  x={barX}
                  y={PADDING.top + drawHeight - sessionHeight}
                  width={barWidth}
                  height={Math.max(sessionHeight, 0)}
                  fill={cssVar("--color-accent-300") || "#8FAFD9"}
                  opacity={dimmed ? 0.2 : 0.65}
                  rx={2}
                  ry={2}
                  className="transition-opacity duration-150"
                />
                {/* Message bar (brand-500, in front) */}
                <rect
                  x={barX}
                  y={PADDING.top + drawHeight - messageHeight}
                  width={barWidth}
                  height={Math.max(messageHeight, 0)}
                  fill={cssVar("--color-brand-500") || "#14488C"}
                  opacity={dimmed ? 0.3 : 1}
                  rx={2}
                  ry={2}
                  className="transition-opacity duration-150 cursor-pointer"
                  onMouseEnter={() => {
                    const centerX = barX + barWidth / 2;
                    setHoveredBar({
                      index: i,
                      value: point.message_count,
                      sessionValue: point.session_count,
                      date: point.date,
                      x: centerX,
                    });
                  }}
                  onMouseLeave={() => setHoveredBar(null)}
                />
                {/* X-axis label (rotated, skipped for dense data) */}
                {i % labelStep === 0 && (
                  <text
                    x={PADDING.left + i * barSlotWidth + barSlotWidth / 2}
                    y={CHART_HEIGHT - 6}
                    textAnchor="end"
                    transform={`rotate(-35, ${PADDING.left + i * barSlotWidth + barSlotWidth / 2}, ${CHART_HEIGHT - 6})`}
                    fill={cssVar("--color-surface-500") || "#6C7A8E"}
                    fontSize={10}
                    fontFamily="var(--font-sans)"
                  >
                    {abbrevDate(point.date)}
                  </text>
                )}
              </g>
            );
          })}
        </svg>

        {/* ── Tooltip ──────────────────────────────────────────────────── */}
        {hoveredBar !== null && chartWidth > 0 && (
          <div
            className="absolute pointer-events-none z-10 animate-fade-in"
            style={{
              left: Math.max(
                0,
                Math.min(hoveredBar.x - 64, chartWidth - 140),
              ),
              top: PADDING.top - 4,
            }}
          >
            <div className="card-base p-2.5 shadow-lg shadow-black/40 text-xs space-y-1.5 min-w-[130px]">
              <p className="text-surface-400 font-medium border-b border-surface-800 pb-1.5 mb-1">
                {(() => {
                  const d = new Date(hoveredBar.date);
                  return d.toLocaleDateString("en-US", {
                    weekday: "short",
                    month: "short",
                    day: "numeric",
                    year: "numeric",
                  });
                })()}
              </p>
              <div className="flex items-center gap-2">
                <span
                  className="h-2.5 w-2.5 rounded-sm shrink-0"
                  style={{ backgroundColor: cssVar("--color-brand-500") || "#14488C" }}
                />
                <span className="text-surface-200">
                  Messages:{" "}
                  <span className="font-semibold font-mono">
                    {hoveredBar.value.toLocaleString()}
                  </span>
                </span>
              </div>
              <div className="flex items-center gap-2">
                <span
                  className="h-2.5 w-2.5 rounded-sm shrink-0"
                  style={{ backgroundColor: cssVar("--color-accent-300") || "#8FAFD9" }}
                />
                <span className="text-surface-200">
                  Sessions:{" "}
                  <span className="font-semibold font-mono">
                    {hoveredBar.sessionValue.toLocaleString()}
                  </span>
                </span>
              </div>
            </div>
          </div>
        )}
      </div>
    );
  }

  // ── Render ───────────────────────────────────────────────────────────

  return (
    <RequireAuth>
    <div className="space-y-6 animate-fade-in">
      {/* Page header */}
      <div>
        <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
          <BarChart3 size={24} className="text-brand-300" />
          Analytics
        </h1>
        <p className="text-sm text-surface-400 mt-1">
          Usage and performance trends
        </p>
      </div>

      {/* ── Stat cards ──────────────────────────────────────────────────── */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        {STAT_CARDS.map((card) => (
          <div key={card.key} className="stat-card">
            <div className="flex items-center gap-3">
              <div className="flex h-12 w-12 items-center justify-center rounded-full bg-brand-500/10">
                <card.icon size={22} className={card.color} />
              </div>
              <div>
                <div className="text-xs text-surface-400">{card.label}</div>
                {loading ? (
                  <div className="h-7 w-20 mt-1 rounded bg-surface-800 animate-pulse" />
                ) : (
                  <div className="text-xl font-semibold mt-0.5 flex items-center gap-2">
                    {stats?.[card.key]?.toLocaleString() ?? (
                      <span className="text-surface-600">—</span>
                    )}
                  </div>
                )}
              </div>
            </div>
          </div>
        ))}
      </div>

      {/* ── Chart section ────────────────────────────────────────────────── */}
      <div className="card-base p-5">
        {/* Header row */}
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-sm font-medium flex items-center gap-1.5">
            <TrendingUp size={16} className="text-brand-300" />
            Daily Usage
          </h3>

          {/* Time range toggle */}
          <div className="flex gap-1 rounded-lg bg-surface-950 p-0.5 border border-surface-800">
            {DAYS_OPTIONS.map((d) => (
              <button
                key={d}
                onClick={() => setDays(d)}
                className={cn(
                  "px-3 py-1.5 text-xs font-medium rounded-md transition-colors",
                  days === d
                    ? "bg-brand-500 text-white shadow-sm"
                    : "text-surface-400 hover:text-surface-100 hover:bg-surface-800",
                )}
              >
                {d}d
              </button>
            ))}
          </div>
        </div>

        {/* Chart area */}
        <div ref={chartRef}>{renderChart()}</div>

        {/* Legend */}
        {usage.length > 0 && !usageLoading && (
          <div className="flex items-center gap-5 mt-3 pt-3 border-t border-surface-800">
            <div className="flex items-center gap-1.5 text-xs text-surface-400">
              <span
                className="h-2.5 w-2.5 rounded-sm shrink-0"
                style={{ backgroundColor: cssVar("--color-brand-500") || "#14488C" }}
              />
              Messages
            </div>
            <div className="flex items-center gap-1.5 text-xs text-surface-400">
              <span
                className="h-2.5 w-2.5 rounded-sm shrink-0"
                style={{ backgroundColor: cssVar("--color-accent-300") || "#8FAFD9" }}
              />
              Sessions
            </div>
            {usageLoading && (
              <div className="flex items-center gap-1.5 text-xs text-surface-500 ml-auto">
                <div className="h-3 w-3 rounded-full border-2 border-surface-500 border-t-transparent animate-spin" />
                Updating...
              </div>
            )}
          </div>
        )}
      </div>

      {/* ── Additional metrics row ───────────────────────────────────────── */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <div className="card-base p-4 flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-full bg-brand-500/10">
            <Users size={18} className="text-surface-300" />
          </div>
          <div>
            <div className="text-xs text-surface-400">Total Users</div>
            {loading ? (
              <div className="h-5 w-14 mt-1 rounded bg-surface-800 animate-pulse" />
            ) : (
              <div className="text-base font-semibold mt-0.5">
                {stats?.total_users?.toLocaleString() ?? "—"}
              </div>
            )}
          </div>
        </div>
        <div className="card-base p-4 flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-full bg-brand-500/10">
            <MessageSquare size={18} className="text-accent-300" />
          </div>
          <div>
            <div className="text-xs text-surface-400">Total Episodes</div>
            {loading ? (
              <div className="h-5 w-14 mt-1 rounded bg-surface-800 animate-pulse" />
            ) : (
              <div className="text-base font-semibold mt-0.5">
                {stats?.total_episodes?.toLocaleString() ?? "—"}
              </div>
            )}
          </div>
        </div>
        <div className="card-base p-4 flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-full bg-brand-500/10">
            <Database size={18} className="text-success" />
          </div>
          <div>
            <div className="text-xs text-surface-400">API Keys</div>
            {loading ? (
              <div className="h-5 w-14 mt-1 rounded bg-surface-800 animate-pulse" />
            ) : (
              <div className="text-base font-semibold mt-0.5">
                {stats?.total_api_keys?.toLocaleString() ?? "—"}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  </RequireAuth>
  );
}
