"use client";

import { useEffect, useState } from "react";
import {
  Box,
  Card,
  CardContent,
  Typography,
  Skeleton,
} from "@mui/material";
import PeopleIcon from "@mui/icons-material/People";
import ChatIcon from "@mui/icons-material/Chat";
import MessageIcon from "@mui/icons-material/Message";
import VpnKeyIcon from "@mui/icons-material/VpnKey";
import { getOrgStats } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";

type OrgStats = components["schemas"]["OrgStatsResponse"];

const STAT_CARDS = [
  { label: "Total Users", key: "total_users" as const, icon: <PeopleIcon />, color: "#1565C0" },
  { label: "Total Sessions", key: "total_sessions" as const, icon: <ChatIcon />, color: "#388E3C" },
  { label: "Total Messages", key: "total_messages" as const, icon: <MessageIcon />, color: "#F57C00" },
  { label: "API Keys", key: "total_api_keys" as const, icon: <VpnKeyIcon />, color: "#7B1FA2" },
];

export default function DashboardOverviewPage() {
  const [stats, setStats] = useState<OrgStats | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    getOrgStats()
      .then(setStats)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  return (
    <Box>
      <Typography variant="h4" gutterBottom>
        Overview
      </Typography>
      <Typography variant="subtitle1" sx={{ mb: 3 }}>
        Summary of your organization
      </Typography>

      <Box
        sx={{
          display: "grid",
          gridTemplateColumns: { xs: "1fr", sm: "1fr 1fr", lg: "1fr 1fr 1fr 1fr" },
          gap: 3,
        }}
      >
        {STAT_CARDS.map((card) => (
          <Card key={card.key}>
            <CardContent sx={{ p: 3 }}>
              <Box sx={{ display: "flex", alignItems: "center", gap: 2 }}>
                <Box
                  sx={{
                    width: 48,
                    height: 48,
                    borderRadius: 2,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    bgcolor: `${card.color}14`,
                    color: card.color,
                  }}
                >
                  {card.icon}
                </Box>
                <Box>
                  <Typography variant="body2" color="text.secondary">
                    {card.label}
                  </Typography>
                  {loading ? (
                    <Skeleton width={60} height={32} />
                  ) : (
                    <Typography variant="h5" sx={{ fontWeight: 600 }}>
                      {stats?.[card.key] ?? "—"}
                    </Typography>
                  )}
                </Box>
              </Box>
            </CardContent>
          </Card>
        ))}
      </Box>
    </Box>
  );
}
