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
  IconButton,
  Tooltip,
  Skeleton,
  Snackbar,
  Alert,
} from "@mui/material";
import ArrowBackIcon from "@mui/icons-material/ArrowBack";
import { getSession, ApiError } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import MessagesTab from "./MessagesTab";
import FactsTab from "./FactsTab";
import GraphTab from "./GraphTab";

type SessionResponse = components["schemas"]["SessionResponse"];

interface SnackbarState {
  open: boolean;
  message: string;
  severity: "success" | "error";
}

export default function SessionDetailPage() {
  const params = useParams();
  const searchParams = useSearchParams();
  const router = useRouter();
  const sessionId = params.id as string;
  const userId = searchParams.get("userId") ?? "";
  const projectId = searchParams.get("projectId") ?? "";

  const [session, setSession] = useState<SessionResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState(0);

  const [snackbar, setSnackbar] = useState<SnackbarState>({
    open: false,
    message: "",
    severity: "success",
  });

  const showSnackbar = useCallback(
    (message: string, severity: "success" | "error") => {
      setSnackbar({ open: true, message, severity });
    },
    [],
  );

  const fetchSession = useCallback(async () => {
    if (!userId || !projectId) return;
    setLoading(true);
    try {
      const result = await getSession(projectId, userId, sessionId);
      setSession(result);
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.detail ?? "Failed to load session"
          : "An unexpected error occurred";
      showSnackbar(message, "error");
    } finally {
      setLoading(false);
    }
  }, [userId, projectId, sessionId, showSnackbar]);

  useEffect(() => {
    fetchSession();
  }, [fetchSession]);

  return (
    <Box>
      {/* Header */}
      <Box sx={{ display: "flex", alignItems: "center", gap: 2, mb: 3 }}>
        <Tooltip title="Back to sessions">
          <IconButton onClick={() => router.push("/dashboard/sessions")}>
            <ArrowBackIcon />
          </IconButton>
        </Tooltip>
        <Box sx={{ flex: 1 }}>
          {loading ? (
            <Skeleton width={300} height={32} />
          ) : (
            <Box sx={{ display: "flex", alignItems: "center", gap: 2 }}>
              <Typography variant="h5" sx={{ fontWeight: 600 }}>
                {session?.external_id ?? "Session"}
              </Typography>
              <Chip
                label={session?.is_active ? "Active" : "Closed"}
                color={session?.is_active ? "success" : "default"}
                size="small"
              />
              {session?.message_count !== undefined && (
                <Typography variant="body2" color="text.secondary">
                  {session.message_count} messages
                  {session.fact_count !== undefined && ` · ${session.fact_count} facts`}
                </Typography>
              )}
            </Box>
          )}
        </Box>
      </Box>

      {/* Session metadata card */}
      {session && (
        <Card sx={{ p: 2, mb: 3 }}>
          <Box sx={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))", gap: 2 }}>
            <Box>
              <Typography variant="caption" color="text.secondary">Session ID</Typography>
              <Typography variant="body2" sx={{ fontFamily: "monospace", fontSize: "0.8rem" }}>
                {session.id}
              </Typography>
            </Box>
            <Box>
              <Typography variant="caption" color="text.secondary">User ID</Typography>
              <Typography variant="body2" sx={{ fontFamily: "monospace", fontSize: "0.8rem" }}>
                {session.user_id}
              </Typography>
            </Box>
            <Box>
              <Typography variant="caption" color="text.secondary">Created</Typography>
              <Typography variant="body2">
                {new Date(session.created_at).toLocaleString()}
              </Typography>
            </Box>
            {session.closed_at && (
              <Box>
                <Typography variant="caption" color="text.secondary">Closed</Typography>
                <Typography variant="body2">
                  {new Date(session.closed_at).toLocaleString()}
                </Typography>
              </Box>
            )}
            {session.metadata && Object.keys(session.metadata).length > 0 && (
              <Box sx={{ gridColumn: "1 / -1" }}>
                <Typography variant="caption" color="text.secondary">Metadata</Typography>
                <Typography variant="body2" sx={{ fontFamily: "monospace", fontSize: "0.8rem" }}>
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
      {tab === 0 && userId && projectId && <MessagesTab projectId={projectId} userId={userId} sessionId={sessionId} />}
      {tab === 1 && userId && projectId && <FactsTab projectId={projectId} userId={userId} sessionId={sessionId} />}
      {tab === 2 && userId && projectId && <GraphTab projectId={projectId} userId={userId} sessionId={sessionId} />}

      {/* Snackbar */}
      <Snackbar
        open={snackbar.open}
        autoHideDuration={4000}
        onClose={() => setSnackbar((prev) => ({ ...prev, open: false }))}
        anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
      >
        <Alert
          onClose={() => setSnackbar((prev) => ({ ...prev, open: false }))}
          severity={snackbar.severity}
          variant="filled"
          sx={{ width: "100%" }}
        >
          {snackbar.message}
        </Alert>
      </Snackbar>
    </Box>
  );
}
