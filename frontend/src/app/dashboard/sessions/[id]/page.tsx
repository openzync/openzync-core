"use client";

import { useState, useEffect, useCallback } from "react";
import { useParams, useSearchParams, useRouter } from "next/navigation";
import {
  Box,
  Typography,
  Tabs,
  Tab,
  Card,
  Chip,
} from "@mui/material";
import { getSession, ApiError } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { useNotification } from "@/components/shared/NotificationProvider";
import PageHeader from "@/components/shared/PageHeader";
import LoadingSkeleton from "@/components/shared/LoadingSkeleton";
import MessagesTab from "./MessagesTab";
import FactsTab from "./FactsTab";
import GraphTab from "./GraphTab";

type SessionResponse = components["schemas"]["SessionResponse"];

export default function SessionDetailPage() {
  const params = useParams();
  const searchParams = useSearchParams();
  const router = useRouter();
  const { showNotification } = useNotification();
  const sessionId = params.id as string;
  const userId = searchParams.get("userId") ?? "";

  const [session, setSession] = useState<SessionResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState(0);

  const fetchSession = useCallback(async () => {
    if (!userId) return;
    setLoading(true);
    try {
      const result = await getSession(userId, sessionId);
      setSession(result);
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.detail ?? "Failed to load session"
          : "An unexpected error occurred";
      showNotification(message, "error");
    } finally {
      setLoading(false);
    }
  }, [userId, sessionId, showNotification]);

  useEffect(() => {
    fetchSession();
  }, [fetchSession]);

  // ── Breadcrumbs ──────────────────────────────────────────────────────────────

  const breadcrumbs = [
    { label: "Sessions", href: "/dashboard/sessions" },
    { label: session?.external_id ?? "Session Detail" },
  ];

  // ── Render ──────────────────────────────────────────────────────────────────

  if (loading) {
    return (
      <Box>
        <PageHeader title="Session" breadcrumbs={breadcrumbs} />
        <LoadingSkeleton variant="detail" count={4} />
      </Box>
    );
  }

  return (
    <Box>
      <PageHeader
        title={session?.external_id ?? "Session Detail"}
        breadcrumbs={breadcrumbs}
        action={
          session && (
            <Box sx={{ display: "flex", alignItems: "center", gap: 2 }}>
              <Chip
                label={session.is_active ? "Active" : "Closed"}
                color={session.is_active ? "success" : "default"}
                size="small"
              />
              {session.message_count !== undefined && (
                <Typography variant="body2" color="text.secondary">
                  {session.message_count} messages
                  {session.fact_count !== undefined && ` · ${session.fact_count} facts`}
                </Typography>
              )}
            </Box>
          )
        }
      />

      {/* Session metadata card */}
      {session && (
        <Card sx={{ p: 2, mb: 3 }}>
          <Box
            sx={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))",
              gap: 2,
            }}
          >
            <Box>
              <Typography variant="caption" color="text.secondary">
                Session ID
              </Typography>
              <Typography variant="body2" sx={{ fontFamily: "monospace", fontSize: "0.8rem" }}>
                {session.id}
              </Typography>
            </Box>
            <Box>
              <Typography variant="caption" color="text.secondary">
                User ID
              </Typography>
              <Typography variant="body2" sx={{ fontFamily: "monospace", fontSize: "0.8rem" }}>
                {session.user_id}
              </Typography>
            </Box>
            <Box>
              <Typography variant="caption" color="text.secondary">
                Created
              </Typography>
              <Typography variant="body2">
                {new Date(session.created_at).toLocaleString()}
              </Typography>
            </Box>
            {session.closed_at && (
              <Box>
                <Typography variant="caption" color="text.secondary">
                  Closed
                </Typography>
                <Typography variant="body2">
                  {new Date(session.closed_at).toLocaleString()}
                </Typography>
              </Box>
            )}
            {session.metadata && Object.keys(session.metadata).length > 0 && (
              <Box sx={{ gridColumn: "1 / -1" }}>
                <Typography variant="caption" color="text.secondary">
                  Metadata
                </Typography>
                <Typography
                  variant="body2"
                  sx={{ fontFamily: "monospace", fontSize: "0.8rem", whiteSpace: "pre-wrap" }}
                >
                  {JSON.stringify(session.metadata, null, 2)}
                </Typography>
              </Box>
            )}
          </Box>
        </Card>
      )}

      {/* Tabs */}
      <Card sx={{ mb: 3 }}>
        <Tabs value={tab} onChange={(_e, v) => setTab(v)} sx={{ px: 2 }}>
          <Tab label="Messages" />
          <Tab label="Facts" />
          <Tab label="Graph" />
        </Tabs>
      </Card>

      {/* Tab panels */}
      {tab === 0 && userId && <MessagesTab userId={userId} sessionId={sessionId} />}
      {tab === 1 && userId && <FactsTab userId={userId} sessionId={sessionId} />}
      {tab === 2 && userId && <GraphTab userId={userId} sessionId={sessionId} />}
    </Box>
  );
}
