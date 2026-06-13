"use client";

import { useState, useCallback, useEffect } from "react";
import {
  Box,
  Button,
  Card,
  Chip,
  Typography,
  CircularProgress,
  Tooltip,
} from "@mui/material";
import { DataGrid, type GridColDef } from "@mui/x-data-grid";
import { getSessionFacts, ApiError } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";

type FactResponse = components["schemas"]["FactResponse"];

interface FactRow {
  id: string;
  content: string;
  subject: string | null;
  predicate: string | null;
  object: string | null;
  confidence: number;
  source_episode_id: string | null;
  subject_type: string;
  object_type: string;
  created_at: string;
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function formatDate(raw: string): string {
  try {
    return new Date(raw).toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  } catch {
    return raw;
  }
}

function confidenceColor(score: number): "success" | "warning" | "error" {
  if (score >= 0.8) return "success";
  if (score >= 0.5) return "warning";
  return "error";
}

// ─── Component Props ──────────────────────────────────────────────────────────

interface FactsTabProps {
  projectId: string;
  userId: string;
  sessionId: string;
}

// ─── Component ────────────────────────────────────────────────────────────────

export default function FactsTab({ projectId, userId, sessionId }: FactsTabProps) {
  const [facts, setFacts] = useState<FactRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);

  // ── Fetch facts ─────────────────────────────────────────────────────────────

  const fetchFacts = useCallback(async () => {
    setLoading(true);
    try {
      const result = await getSessionFacts(projectId, userId, sessionId, { limit: 50 });
      setFacts(result.data as FactRow[]);
      setCursor(result.next_cursor ?? null);
      setHasMore(result.has_more);
    } catch (err) {
      console.error("Failed to load facts", err);
    } finally {
      setLoading(false);
    }
  }, [projectId, userId, sessionId]);

  const loadMore = useCallback(async () => {
    if (loadingMore || !cursor) return;
    setLoadingMore(true);
    try {
      const result = await getSessionFacts(projectId, userId, sessionId, {
        limit: 50,
        cursor,
      });
      setFacts((prev) => [...prev, ...(result.data as FactRow[])]);
      setCursor(result.next_cursor ?? null);
      setHasMore(result.has_more);
    } catch (err) {
      console.error("Failed to load more facts", err);
    } finally {
      setLoadingMore(false);
    }
  }, [loadingMore, cursor, projectId, userId, sessionId]);

  useEffect(() => {
    fetchFacts();
  }, [fetchFacts]);

  // ── Columns ─────────────────────────────────────────────────────────────────

  const columns: GridColDef<FactRow>[] = [
    {
      field: "content",
      headerName: "Fact",
      flex: 2,
      minWidth: 300,
      renderCell: ({ row }) => (
        <Box>
          <Typography variant="body2" sx={{ fontWeight: 500 }}>
            {row.content}
          </Typography>
          {(row.subject || row.predicate || row.object) && (
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.25 }}>
              {row.subject ?? "?"} → {row.predicate ?? "?"} → {row.object ?? "?"}
            </Typography>
          )}
        </Box>
      ),
    },
    {
      field: "subject_type",
      headerName: "Subject Type",
      width: 120,
      renderCell: ({ value }) => (
        <Chip label={value} size="small" variant="outlined" />
      ),
    },
    {
      field: "confidence",
      headerName: "Confidence",
      width: 120,
      align: "center",
      renderCell: ({ value }) => (
        <Chip
          label={`${(value * 100).toFixed(0)}%`}
          size="small"
          color={confidenceColor(value as number)}
        />
      ),
    },
    {
      field: "source_episode_id",
      headerName: "Source",
      width: 100,
      renderCell: ({ value }) =>
        value ? (
          <Tooltip title={value as string}>
            <Typography variant="caption" sx={{ fontFamily: "monospace", fontSize: "0.7rem" }}>
              {(value as string).slice(0, 8)}...
            </Typography>
          </Tooltip>
        ) : (
          <Typography variant="caption" color="text.disabled">—</Typography>
        ),
    },
    {
      field: "created_at",
      headerName: "Extracted",
      width: 110,
      valueGetter: (_value, row) => formatDate(row.created_at),
    },
  ];

  // ── Render ──────────────────────────────────────────────────────────────────

  if (loading) {
    return (
      <Box sx={{ display: "flex", justifyContent: "center", py: 6 }}>
        <CircularProgress />
      </Box>
    );
  }

  if (facts.length === 0) {
    return (
      <Card sx={{ p: 4, textAlign: "center" }}>
        <Typography variant="body1" color="text.secondary">
          No facts extracted from this session yet. Facts appear after messages are
          processed by the extraction worker.
        </Typography>
      </Card>
    );
  }

  return (
    <Card>
      <DataGrid<FactRow>
        rows={facts}
        columns={columns}
        loading={loading}
        getRowId={(row) => row.id}
        getRowHeight={() => 68}
        disableRowSelectionOnClick
        hideFooter
        autoHeight
        sx={{
          "& .MuiDataGrid-cell:focus": { outline: "none" },
        }}
      />
      {hasMore && (
        <Box sx={{ display: "flex", justifyContent: "center", p: 2 }}>
          <Button
            variant="outlined"
            onClick={loadMore}
            disabled={loadingMore}
            startIcon={loadingMore ? <CircularProgress size={16} /> : undefined}
          >
            {loadingMore ? "Loading..." : "Load More"}
          </Button>
        </Box>
      )}
    </Card>
  );
}
