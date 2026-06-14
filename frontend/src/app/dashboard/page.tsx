"use client";

import { useEffect, useState, useCallback } from "react";
import {
  Box,
  Card,
  CardContent,
  Typography,
  Stack,
} from "@mui/material";
import PeopleIcon from "@mui/icons-material/People";
import ChatIcon from "@mui/icons-material/Chat";
import MessageIcon from "@mui/icons-material/Message";
import VpnKeyIcon from "@mui/icons-material/VpnKey";
import HistoryIcon from "@mui/icons-material/History";
import {
  getOrgStats,
  listAuditLogs,
  ApiError,
  type AuditLogEntry,
} from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import PageHeader from "@/components/shared/PageHeader";
import LoadingSkeleton from "@/components/shared/LoadingSkeleton";
import { useNotification } from "@/components/shared/NotificationProvider";

type OrgStats = components["schemas"]["OrgStatsResponse"];

const STAT_CARDS = [
  {
    label: "Total Users",
    key: "total_users" as const,
    icon: <PeopleIcon />,
  },
  {
    label: "Total Sessions",
    key: "total_sessions" as const,
    icon: <ChatIcon />,
  },
  {
    label: "Total Messages",
    key: "total_messages" as const,
    icon: <MessageIcon />,
  },
  {
    label: "API Keys",
    key: "total_api_keys" as const,
    icon: <VpnKeyIcon />,
  },
];

function formatTimestamp(raw: string): string {
  try {
    const date = new Date(raw);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / 60000);

    if (diffMins < 1) return "Just now";
    if (diffMins < 60) return `${diffMins}m ago`;

    const diffHours = Math.floor(diffMins / 60);
    if (diffHours < 24) return `${diffHours}h ago`;

    const diffDays = Math.floor(diffHours / 24);
    if (diffDays < 7) return `${diffDays}d ago`;

    return date.toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
      year: "numeric",
    });
  } catch {
    return "—";
  }
}

function actionLabel(entry: AuditLogEntry): string {
  // Human-readable action from the audit log action + resource_type
  const action = entry.action ?? "";
  const resource = entry.resource_type ?? "";
  const method = entry.method ?? "";

  if (action && resource) return `${action} ${resource}`;
  if (method && resource) return `${method} ${resource}`;
  return action || method || "Unknown action";
}

function actorLabel(entry: AuditLogEntry): string {
  // Display actor name, type, or dash
  if (entry.actor_id) {
    // Show last 8 chars of actor UUID for brevity
    return entry.actor_id.length > 12
      ? `...${entry.actor_id.slice(-8)}`
      : entry.actor_id;
  }
  return entry.actor_type ?? "System";
}

export default function DashboardOverviewPage() {
  const [stats, setStats] = useState<OrgStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [auditLogs, setAuditLogs] = useState<AuditLogEntry[]>([]);
  const [auditLogsLoading, setAuditLogsLoading] = useState(true);
  const { showNotification } = useNotification();

  useEffect(() => {
    getOrgStats()
      .then(setStats)
      .catch((err) => {
        const message =
          err instanceof ApiError
            ? err.detail ?? "Failed to load stats"
            : "An unexpected error occurred";
        showNotification(message, "error");
      })
      .finally(() => setLoading(false));
  }, [showNotification]);

  const fetchAuditLogs = useCallback(async () => {
    setAuditLogsLoading(true);
    try {
      const result = await listAuditLogs({ limit: 5 });
      setAuditLogs(result.items);
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.detail ?? "Failed to load recent activity"
          : "An unexpected error occurred";
      showNotification(message, "error");
    } finally {
      setAuditLogsLoading(false);
    }
  }, [showNotification]);

  useEffect(() => {
    fetchAuditLogs();
  }, [fetchAuditLogs]);

  return (
    <Box>
      <PageHeader
        title="Overview"
        subtitle="Summary of your organization"
      />

      {/* ── Stat Cards ───────────────────────────────────────────────────── */}
      {loading ? (
        <LoadingSkeleton variant="card" count={4} />
      ) : (
        <Box
          sx={{
            display: "grid",
            gridTemplateColumns: {
              xs: "1fr",
              sm: "1fr 1fr",
              lg: "1fr 1fr 1fr 1fr",
            },
            gap: 3,
          }}
        >
          {STAT_CARDS.map((card) => (
            <Card
              key={card.key}
              sx={{
                transition: "transform 0.2s, box-shadow 0.2s",
                border: "1px solid",
                borderColor: "divider",
                "&:hover": {
                  transform: "translateY(-2px)",
                  boxShadow: (theme) => theme.shadows[4],
                },
              }}
            >
              <CardContent sx={{ p: 3 }}>
                <Box
                  sx={{ display: "flex", alignItems: "center", gap: 2 }}
                >
                  <Box
                    sx={{
                      width: 48,
                      height: 48,
                      borderRadius: "50%",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      bgcolor: "primary.main",
                      color: "primary.contrastText",
                      opacity: 0.9,
                    }}
                  >
                    {card.icon}
                  </Box>
                  <Box>
                    <Typography
                      variant="body2"
                      sx={{ color: "text.secondary", fontWeight: 500 }}
                    >
                      {card.label}
                    </Typography>
                    <Typography
                      variant="h5"
                      sx={{ fontWeight: 700, mt: 0.25 }}
                    >
                      {stats?.[card.key] ?? "—"}
                    </Typography>
                  </Box>
                </Box>
              </CardContent>
            </Card>
          ))}
        </Box>
      )}

      {/* ── Recent Activity ──────────────────────────────────────────────── */}
      <Typography
        variant="h6"
        sx={{ mt: 4, mb: 2, fontWeight: 600, display: "flex", alignItems: "center", gap: 1 }}
      >
        <HistoryIcon fontSize="small" />
        Recent Activity
      </Typography>

      {auditLogsLoading ? (
        <LoadingSkeleton variant="table" count={5} />
      ) : auditLogs.length === 0 ? (
        <Card
          sx={{
            p: 4,
            textAlign: "center",
            border: "1px solid",
            borderColor: "divider",
          }}
        >
          <Typography
            variant="body2"
            sx={{ color: "text.secondary" }}
          >
            No recent activity found.
          </Typography>
        </Card>
      ) : (
        <Card
          sx={{
            border: "1px solid",
            borderColor: "divider",
          }}
        >
          <Stack spacing={0} divider={<Box sx={{ height: "1px", bgcolor: "divider" }} />}>
            {auditLogs.map((entry) => (
              <Box
                key={entry.id}
                sx={{
                  display: "flex",
                  alignItems: "center",
                  gap: 2,
                  px: 3,
                  py: 2,
                  transition: "background-color 0.15s",
                  "&:hover": {
                    bgcolor: "action.hover",
                  },
                }}
              >
                {/* Timeline dot */}
                <Box
                  sx={{
                    width: 8,
                    height: 8,
                    borderRadius: "50%",
                    bgcolor: "primary.main",
                    flexShrink: 0,
                    opacity: 0.6,
                  }}
                />
                {/* Action */}
                <Typography
                  variant="body2"
                  sx={{ flex: 1, fontWeight: 500, minWidth: 0 }}
                >
                  {actionLabel(entry)}
                </Typography>
                {/* Actor */}
                <Typography
                  variant="caption"
                  sx={{ color: "text.secondary", flexShrink: 0 }}
                >
                  {actorLabel(entry)}
                </Typography>
                {/* Timestamp */}
                <Typography
                  variant="caption"
                  sx={{ color: "text.disabled", flexShrink: 0, minWidth: 60, textAlign: "right" }}
                >
                  {formatTimestamp(entry.created_at)}
                </Typography>
              </Box>
            ))}
          </Stack>
        </Card>
      )}
    </Box>
  );
}
