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
  Skeleton,
  ToggleButtonGroup,
  ToggleButton,
} from "@mui/material";
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

// ─── Page Component ───────────────────────────────────────────────────────────

export default function AnalyticsPage() {
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
      // Silently fail — UI shows dashes via null coalescing below
    } finally {
      setStatsLoading(false);
    }
  }, []);

  const fetchUsageStats = useCallback(async (d: number) => {
    setChartLoading(true);
    try {
      const data = await getUsageStats(d);
      setUsageData(data);
    } catch {
      // Silently fail — chart area shows empty state
    } finally {
      setChartLoading(false);
    }
  }, []);

  // Initial load of both endpoints
  useEffect(() => {
    fetchOrgStats();
    fetchUsageStats(days);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Re-fetch chart data when the time-range toggle changes
  useEffect(() => {
    fetchUsageStats(days);
  }, [days, fetchUsageStats]);

  const handleDaysChange = (
    _: React.MouseEvent<HTMLElement>,
    newDays: number | null,
  ) => {
    if (newDays !== null) {
      setDays(newDays);
    }
  };

  return (
    <Box>
      {/* ── Page Heading ──────────────────────────────────────────────────── */}
      <Typography variant="h4" sx={{ fontWeight: 600, mb: 3 }}>
        Analytics
      </Typography>

      {/* ── Summary Stat Cards ─────────────────────────────────────────────── */}
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
              <Typography variant="body2" sx={{ color: "text.secondary", mb: 0.5 }}>
                {card.label}
              </Typography>
              {statsLoading ? (
                <Skeleton variant="text" width={80} height={36} />
              ) : (
                <Typography variant="h5" sx={{ fontWeight: 600 }}>
                  {orgStats?.[card.key]?.toLocaleString() ?? "—"}
                </Typography>
              )}
            </CardContent>
          </Card>
        ))}
      </Box>

      {/* ── Daily Usage Chart ──────────────────────────────────────────────── */}
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

          {chartLoading ? (
            <Skeleton
              variant="rectangular"
              width="100%"
              height={400}
              sx={{ borderRadius: 1 }}
            />
          ) : usageData.length === 0 ? (
            <Box
              sx={{
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                height: 400,
              }}
            >
              <Typography variant="body1" sx={{ color: "text.secondary" }}>
                No usage data available for this period.
              </Typography>
            </Box>
          ) : (
            <ResponsiveContainer width="100%" height={400}>
              <BarChart data={usageData}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="date" />
                <YAxis />
                <Tooltip />
                <Legend />
                <Bar
                  dataKey="message_count"
                  name="Messages"
                  fill="#1976d2"
                  radius={[4, 4, 0, 0]}
                />
                <Bar
                  dataKey="session_count"
                  name="Sessions"
                  fill="#dc004e"
                  radius={[4, 4, 0, 0]}
                />
              </BarChart>
            </ResponsiveContainer>
          )}
        </CardContent>
      </Card>
    </Box>
  );
}
