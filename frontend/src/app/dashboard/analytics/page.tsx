"use client";

import { useEffect, useState, useCallback } from "react";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
} from "recharts";
import {
  Box,
  Card,
  CardContent,
  Typography,
  ToggleButtonGroup,
  ToggleButton,
} from "@mui/material";
import PageHeader from "@/components/shared/PageHeader";
import LoadingSkeleton from "@/components/shared/LoadingSkeleton";
import EmptyState from "@/components/shared/EmptyState";
import { useNotification } from "@/components/shared/NotificationProvider";
import { getUsageStats, getOrgStats } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";

// ─── Types ────────────────────────────────────────────────────────────────────

type UsageStat = components["schemas"]["UsageStatsResponse"];
type OrgStats = components["schemas"]["OrgStatsResponse"];

const STAT_CARDS = [
  { label: "Total Messages", key: "total_messages" as const },
  { label: "Total Sessions", key: "total_sessions" as const },
  { label: "Total Facts", key: "total_facts" as const },
] as const;

const DAY_OPTIONS = [7, 30, 90] as const;

// ─── Custom Dark-Themed Chart Tooltip ─────────────────────────────────────────

function CustomChartTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean;
  payload?: Array<{ name: string; value: number; fill: string }>;
  label?: string;
}) {
  if (!active || !payload?.length) return null;
  return (
    <Box
      sx={(theme) => ({
        bgcolor: theme.palette.background.paper,
        border: 1,
        borderColor: theme.palette.divider,
        borderRadius: 1.5,
        p: 1.5,
        boxShadow: "0 4px 20px rgba(0,0,0,0.3)",
      })}
    >
      <Typography
        variant="caption"
        sx={{ color: "text.secondary", mb: 0.5, display: "block" }}
      >
        {label}
      </Typography>
      {payload.map((entry) => (
        <Box
          key={entry.name}
          sx={{ display: "flex", alignItems: "center", gap: 1 }}
        >
          <Box
            sx={{
              width: 8,
              height: 8,
              borderRadius: "50%",
              bgcolor: entry.fill,
            }}
          />
          <Typography variant="body2" sx={{ fontWeight: 500 }}>
            {entry.name}: {entry.value.toLocaleString()}
          </Typography>
        </Box>
      ))}
    </Box>
  );
}

// ─── Page Component ───────────────────────────────────────────────────────────

export default function AnalyticsPage() {
  const { showNotification } = useNotification();

  const [orgStats, setOrgStats] = useState<OrgStats | null>(null);
  const [usageData, setUsageData] = useState<UsageStat[]>([]);
  const [statsLoading, setStatsLoading] = useState(true);
  const [chartLoading, setChartLoading] = useState(true);
  const [days, setDays] = useState<number>(7);

  const fetchOrgStats = useCallback(async () => {
    setStatsLoading(true);
    try {
      const data = await getOrgStats();
      setOrgStats(data);
    } catch {
      showNotification("Failed to load organization stats", "error");
    } finally {
      setStatsLoading(false);
    }
  }, [showNotification]);

  const fetchUsageStats = useCallback(
    async (d: number) => {
      setChartLoading(true);
      try {
        const data = await getUsageStats(d);
        setUsageData(data);
      } catch {
        showNotification("Failed to load usage data", "error");
      } finally {
        setChartLoading(false);
      }
    },
    [showNotification],
  );

  // Initial load
  useEffect(() => {
    fetchOrgStats();
    fetchUsageStats(days);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Re-fetch chart when time-range toggle changes
  useEffect(() => {
    fetchUsageStats(days);
  }, [days, fetchUsageStats]);

  const handleDaysChange = (
    _: React.MouseEvent<HTMLElement>,
    newDays: number | null,
  ) => {
    if (newDays !== null) setDays(newDays);
  };

  return (
    <Box>
      <PageHeader
        title="Analytics"
        subtitle="Usage and performance trends"
      />

      {/* ── Summary Stat Cards ─────────────────────────────────────────────── */}
      {statsLoading ? (
        <Box sx={{ mb: 3 }}>
          <LoadingSkeleton variant="card" count={3} />
        </Box>
      ) : (
        <Box
          sx={{
            display: "flex",
            gap: 3,
            flexWrap: "wrap",
            mb: 3,
          }}
        >
          {STAT_CARDS.map((card) => (
            <Card key={card.key} sx={{ flex: "1 1 200px" }}>
              <CardContent sx={{ p: 3 }}>
                <Typography
                  variant="body2"
                  sx={{ color: "text.secondary", mb: 0.5 }}
                >
                  {card.label}
                </Typography>
                <Typography variant="h5" sx={{ fontWeight: 600 }}>
                  {orgStats?.[card.key]?.toLocaleString() ?? "—"}
                </Typography>
              </CardContent>
            </Card>
          ))}
        </Box>
      )}

      {/* ── Daily Usage Chart ──────────────────────────────────────────────── */}
      {chartLoading ? (
        <LoadingSkeleton variant="chart" />
      ) : usageData.length === 0 ? (
        <Card>
          <EmptyState
            icon={
              <Box
                component="span"
                sx={{ fontSize: 32, lineHeight: 1, color: "text.disabled" }}
              >
                📊
              </Box>
            }
            title="No usage data"
            description="No usage data available for this period."
          />
        </Card>
      ) : (
        <Card>
          <CardContent sx={{ p: 3 }}>
            <Box
              sx={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                mb: 3,
              }}
            >
              <Typography variant="h6" sx={{ fontWeight: 600 }}>
                Daily Usage
              </Typography>

              <ToggleButtonGroup
                value={days}
                exclusive
                onChange={handleDaysChange}
                size="small"
              >
                {DAY_OPTIONS.map((opt) => (
                  <ToggleButton key={opt} value={opt}>
                    {opt}d
                  </ToggleButton>
                ))}
              </ToggleButtonGroup>
            </Box>

            <ResponsiveContainer width="100%" height={400}>
              <BarChart data={usageData}>
                <CartesianGrid
                  strokeDasharray="3 3"
                  stroke="rgba(143,175,217,0.1)"
                />
                <XAxis
                  dataKey="date"
                  tick={{ fill: "rgba(143,175,217,0.6)", fontSize: 12 }}
                  axisLine={{ stroke: "rgba(143,175,217,0.15)" }}
                />
                <YAxis
                  tick={{ fill: "rgba(143,175,217,0.6)", fontSize: 12 }}
                  axisLine={{ stroke: "rgba(143,175,217,0.15)" }}
                />
                <Tooltip content={<CustomChartTooltip />} />
                <Legend
                  wrapperStyle={{ paddingTop: 16 }}
                  formatter={(value: string) => (
                    <span style={{ color: "rgba(143,175,217,0.8)" }}>
                      {value}
                    </span>
                  )}
                />
                <Bar
                  dataKey="message_count"
                  name="Messages"
                  fill="#14488C"
                  radius={[4, 4, 0, 0]}
                />
                <Bar
                  dataKey="session_count"
                  name="Sessions"
                  fill="#8FAFD9"
                  radius={[4, 4, 0, 0]}
                />
              </BarChart>
            </ResponsiveContainer>
          </CardContent>
        </Card>
      )}
    </Box>
  );
}
