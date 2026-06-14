"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import {
  Box,
  Card,
  CardContent,
  Typography,
  Skeleton,
  Chip,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Paper,
} from "@mui/material";
import { useTheme } from "@mui/material/styles";
import TimelineIcon from "@mui/icons-material/Timeline";
import StorageIcon from "@mui/icons-material/Storage";
import HubIcon from "@mui/icons-material/Hub";
import PeopleIcon from "@mui/icons-material/People";
import PageHeader from "@/components/shared/PageHeader";
import LoadingSkeleton from "@/components/shared/LoadingSkeleton";
import {
  getMetricsSummary,
  getMetricsTargets,
  type MetricsSummaryResponse,
  type MetricsTargetsResponse,
} from "@/lib/api/client";

// ─── Constants ────────────────────────────────────────────────────────────────

const REFRESH_INTERVAL_MS = 30_000; // 30 seconds

// ─── Helpers ──────────────────────────────────────────────────────────────────

function timeAgo(date: Date): string {
  const seconds = Math.floor((Date.now() - date.getTime()) / 1000);
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function formatMs(ms: number | null | undefined): string {
  if (ms == null) return "—";
  return `${ms.toFixed(1)} ms`;
}

function formatNumber(n: number): string {
  return n.toLocaleString();
}

// ─── Stat Card Component ──────────────────────────────────────────────────────

function StatCard({
  icon,
  label,
  primary,
  secondary,
  loading,
}: {
  icon: React.ReactNode;
  label: string;
  primary: string;
  secondary?: string;
  loading: boolean;
}) {
  return (
    <Card sx={{ flex: "1 1 200px" }}>
      <CardContent sx={{ p: 3 }}>
        <Box sx={{ display: "flex", alignItems: "center", gap: 2, mb: 1 }}>
          <Box
            sx={(theme) => ({
              width: 40,
              height: 40,
              borderRadius: 2,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              bgcolor: theme.palette.primary.dark,
              color: theme.palette.primary.light,
              opacity: 0.85,
            })}
          >
            {icon}
          </Box>
          <Typography variant="body2" color="text.secondary">
            {label}
          </Typography>
        </Box>
        {loading ? (
          <Skeleton width={100} height={32} />
        ) : (
          <>
            <Typography variant="h5" sx={{ fontWeight: 600 }}>
              {primary}
            </Typography>
            {secondary && (
              <Typography variant="caption" color="text.secondary">
                {secondary}
              </Typography>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

// ─── Latency Card Component ───────────────────────────────────────────────────

function LatencyCard({
  title,
  data,
  loading,
  dotColors,
}: {
  title: string;
  data: { p50: number; p95: number; p99: number } | null | undefined;
  loading: boolean;
  dotColors: { good: string; warn: string; bad: string };
}) {
  const rows = [
    { label: "p50", value: data?.p50 },
    { label: "p95", value: data?.p95 },
    { label: "p99", value: data?.p99 },
  ];

  function latencyColor(ms: number): string {
    if (ms < 100) return dotColors.good;
    if (ms < 500) return dotColors.warn;
    return dotColors.bad;
  }

  return (
    <Card sx={{ flex: "1 1 280px" }}>
      <CardContent sx={{ p: 3 }}>
        <Typography
          variant="subtitle2"
          sx={{ fontWeight: 600, mb: 2, color: "text.secondary" }}
        >
          {title}
        </Typography>
        {loading ? (
          <Box sx={{ display: "flex", flexDirection: "column", gap: 1.5 }}>
            {[1, 2, 3].map((i) => (
              <Skeleton key={i} width="100%" height={28} />
            ))}
          </Box>
        ) : (
          <Box sx={{ display: "flex", flexDirection: "column", gap: 1.5 }}>
            {rows.map((row) => (
              <Box
                key={row.label}
                sx={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                }}
              >
                <Typography
                  variant="body2"
                  sx={{ fontFamily: "monospace", fontWeight: 500 }}
                >
                  {row.label}
                </Typography>
                <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
                  <Box
                    sx={(theme) => ({
                      width: 10,
                      height: 10,
                      borderRadius: "50%",
                      bgcolor:
                        row.value != null
                          ? latencyColor(row.value)
                          : theme.palette.grey[700],
                    })}
                  />
                  <Typography variant="body2" sx={{ fontWeight: 600 }}>
                    {formatMs(row.value)}
                  </Typography>
                </Box>
              </Box>
            ))}
          </Box>
        )}
      </CardContent>
    </Card>
  );
}

// ─── Main Page ────────────────────────────────────────────────────────────────

export default function MonitoringPage() {
  const theme = useTheme();
  const dotColors = {
    good: theme.palette.success.main,
    warn: theme.palette.warning.main,
    bad: theme.palette.error.main,
  };

  const [metrics, setMetrics] = useState<MetricsSummaryResponse | null>(null);
  const [targets, setTargets] = useState<MetricsTargetsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetchData = useCallback(async () => {
    try {
      const [m, t] = await Promise.all([
        getMetricsSummary(),
        getMetricsTargets(),
      ]);
      setMetrics(m);
      setTargets(t);
      setLastUpdated(new Date());
    } catch {
      // Monitoring page: silent fail is intentional — existing data stays visible
      console.error("[monitoring] Failed to fetch metrics data");
    } finally {
      setLoading(false);
    }
  }, []);

  // Initial fetch
  useEffect(() => {
    fetchData();
  }, [fetchData]);

  // Auto-refresh every 30s
  useEffect(() => {
    intervalRef.current = setInterval(fetchData, REFRESH_INTERVAL_MS);
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [fetchData]);

  const ep = metrics?.episodes;
  const gr = metrics?.graphs;
  const ol = metrics?.overall_latency_ms;
  const cl = metrics?.context_latency_ms;
  const gs = metrics?.graph_search_latency_ms;
  const qd = metrics?.queue_depth;

  return (
    <Box>
      <PageHeader
        title="Monitoring"
        subtitle="Real-time platform performance and health metrics"
        action={
          lastUpdated && !loading ? (
            <Typography variant="caption" sx={{ color: "text.secondary", mt: 0.5 }}>
              Last updated {timeAgo(lastUpdated)}
            </Typography>
          ) : undefined
        }
      />

      {/* ── Row 1: Stat Cards ────────────────────────────────────────────── */}
      <Typography variant="h6" sx={{ fontWeight: 600, mb: 2 }}>
        Data Snapshot
      </Typography>
      <Box sx={{ display: "flex", gap: 3, flexWrap: "wrap", mb: 4 }}>
        <StatCard
          icon={<TimelineIcon />}
          label="Episodes Added"
          primary={formatNumber(ep?.added_total ?? 0)}
          secondary={ep ? `+${formatNumber(ep.added_24h)} in last 24h` : undefined}
          loading={loading}
        />
        <StatCard
          icon={<StorageIcon />}
          label="Episodes In Progress"
          primary={formatNumber(ep?.in_progress ?? 0)}
          secondary={ep ? `${formatNumber(ep.enrichment_pending)} pending enrichment` : undefined}
          loading={loading}
        />
        <StatCard
          icon={<HubIcon />}
          label="Graphs Created"
          primary={formatNumber(gr?.entities_total ?? 0)}
          secondary={gr ? `+${formatNumber(gr.entities_24h)} in last 24h` : undefined}
          loading={loading}
        />
        <StatCard
          icon={<PeopleIcon />}
          label="Users Created"
          primary={formatNumber(metrics?.users_total ?? 0)}
          loading={loading}
        />
      </Box>

      {/* ── Row 2: Latency Panel ─────────────────────────────────────────── */}
      <Typography variant="h6" sx={{ fontWeight: 600, mb: 2 }}>
        Latency (ms)
      </Typography>
      <Box sx={{ display: "flex", gap: 3, flexWrap: "wrap", mb: 4 }}>
        <LatencyCard title="Overall API" data={ol} loading={loading} dotColors={dotColors} />
        <LatencyCard title="Context Assembly" data={cl} loading={loading} dotColors={dotColors} />
        <LatencyCard title="Graph Search" data={gs} loading={loading} dotColors={dotColors} />
      </Box>

      {/* ── Row 3: Error Rate & Health ───────────────────────────────────── */}
      <Typography variant="h6" sx={{ fontWeight: 600, mb: 2 }}>
        System Health
      </Typography>
      <Box sx={{ display: "flex", gap: 3, flexWrap: "wrap", mb: 4 }}>
        {/* Error Rate */}
        <Card sx={{ flex: "1 1 200px" }}>
          <CardContent sx={{ p: 3 }}>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
              Error Rate
            </Typography>
            {loading ? (
              <Skeleton width={80} height={40} />
            ) : (
              <>
                <Typography
                  variant="h4"
                  sx={{
                    fontWeight: 700,
                    color:
                      (metrics?.error_rate_pct ?? 0) < 1
                        ? "success.main"
                        : (metrics?.error_rate_pct ?? 0) < 5
                          ? "warning.main"
                          : "error.main",
                  }}
                >
                  {(metrics?.error_rate_pct ?? 0).toFixed(2)}%
                </Typography>
                <Box sx={{ display: "flex", gap: 2, mt: 1 }}>
                  <Typography variant="caption" color="text.secondary">
                    2xx: {formatNumber(metrics?.request_rate?.["2xx"] ?? 0)}/s
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    5xx: {formatNumber(metrics?.request_rate?.["5xx"] ?? 0)}/s
                  </Typography>
                </Box>
              </>
            )}
          </CardContent>
        </Card>

        {/* Queue Depth */}
        <Card sx={{ flex: "1 1 200px" }}>
          <CardContent sx={{ p: 3 }}>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
              Queue Depth
            </Typography>
            {loading ? (
              <Skeleton width={80} height={40} />
            ) : (
              <>
                <Typography variant="h4" sx={{ fontWeight: 700 }}>
                  {qd ? formatNumber(qd.high + qd.low) : "—"}
                </Typography>
                <Box sx={{ display: "flex", gap: 2, mt: 1 }}>
                  <Typography variant="caption" color="text.secondary">
                    High: {formatNumber(qd?.high ?? 0)}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    Low: {formatNumber(qd?.low ?? 0)}
                  </Typography>
                </Box>
              </>
            )}
          </CardContent>
        </Card>

        {/* Active Requests */}
        <Card sx={{ flex: "1 1 200px" }}>
          <CardContent sx={{ p: 3 }}>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
              Active Requests
            </Typography>
            {loading ? (
              <Skeleton width={80} height={40} />
            ) : (
              <Typography variant="h4" sx={{ fontWeight: 700 }}>
                {formatNumber(metrics?.active_requests ?? 0)}
              </Typography>
            )}
          </CardContent>
        </Card>

        {/* Prometheus Status */}
        <Card sx={{ flex: "1 1 200px" }}>
          <CardContent sx={{ p: 3 }}>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
              Prometheus
            </Typography>
            {loading ? (
              <Skeleton width={80} height={40} />
            ) : (
              <>
                <Chip
                  label={metrics?.status ?? "unknown"}
                  color={metrics?.status === "ok" ? "success" : "warning"}
                  size="small"
                  sx={{ fontWeight: 600, mb: 1 }}
                />
                {metrics?.message && (
                  <Typography
                    variant="caption"
                    color="text.secondary"
                    sx={{ display: "block" }}
                  >
                    {metrics.message}
                  </Typography>
                )}
              </>
            )}
          </CardContent>
        </Card>
      </Box>

      {/* ── Row 4: Prometheus Targets ────────────────────────────────────── */}
      <Typography variant="h6" sx={{ fontWeight: 600, mb: 2 }}>
        Scrape Targets
      </Typography>

      {loading ? (
        <LoadingSkeleton variant="table" />
      ) : !targets || targets.targets.length === 0 ? (
        <Card>
          <Box sx={{ p: 3 }}>
            <Typography variant="body2" color="text.secondary">
              No scrape targets found.
            </Typography>
          </Box>
        </Card>
      ) : (
        <Card>
          <CardContent sx={{ p: 0 }}>
            <TableContainer component={Paper} elevation={0}>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell sx={{ fontWeight: 600 }}>Job</TableCell>
                    <TableCell sx={{ fontWeight: 600 }}>Instance</TableCell>
                    <TableCell sx={{ fontWeight: 600 }}>Health</TableCell>
                    <TableCell sx={{ fontWeight: 600 }}>Last Scrape</TableCell>
                    <TableCell sx={{ fontWeight: 600 }}>Last Error</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {targets.targets.map((t, i) => (
                    <TableRow key={i}>
                      <TableCell>
                        <Typography
                          variant="body2"
                          sx={{ fontFamily: "monospace" }}
                        >
                          {t.job}
                        </Typography>
                      </TableCell>
                      <TableCell>
                        <Typography
                          variant="body2"
                          sx={{ fontFamily: "monospace" }}
                        >
                          {t.instance}
                        </Typography>
                      </TableCell>
                      <TableCell>
                        <Chip
                          label={t.health}
                          color={t.health === "up" ? "success" : "error"}
                          size="small"
                          variant="outlined"
                        />
                      </TableCell>
                      <TableCell>
                        <Typography variant="body2" color="text.secondary">
                          {t.last_scrape
                            ? timeAgo(new Date(t.last_scrape))
                            : "—"}
                        </Typography>
                      </TableCell>
                      <TableCell>
                        <Typography
                          variant="body2"
                          color="error"
                          sx={{
                            maxWidth: 300,
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                          }}
                        >
                          {t.last_error ?? "—"}
                        </Typography>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
          </CardContent>
        </Card>
      )}
    </Box>
  );
}
