"use client";

import { useState, useCallback, useEffect } from "react";
import { useRouter } from "next/navigation";
import {
  Box,
  Button,
  Card,
  Chip,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  TextField,
  Typography,
  IconButton,
  Tooltip,
  Snackbar,
  Alert,
  CircularProgress,
} from "@mui/material";
import { DataGrid, type GridColDef } from "@mui/x-data-grid";
import {
  Add as AddIcon,
  Visibility as ViewIcon,
  Delete as DeleteIcon,
} from "@mui/icons-material";
import {
  listSessions,
  createSession,
  deleteSession,
  listUsers,
  ApiError,
} from "@/lib/api/client";
import { useAuth } from "@/lib/auth/useAuth";

// ─── Types ────────────────────────────────────────────────────────────────────

interface SessionRow {
  id: string;
  user_id: string;
  external_id: string;
  is_active: boolean;
  message_count: number;
  fact_count: number;
  created_at: string;
}

interface SnackbarState {
  open: boolean;
  message: string;
  severity: "success" | "error";
}

// ─── Page Component ───────────────────────────────────────────────────────────

export default function SessionsPage() {
  useAuth();
  const router = useRouter();

  // ── Data state ──────────────────────────────────────────────────────────────

  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);

  // ── Create dialog ──────────────────────────────────────────────────────────

  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [userId, setUserId] = useState("");
  const [externalId, setExternalId] = useState("");
  const [users, setUsers] = useState<{ id: string; external_id: string }[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState("");

  // ── Delete dialog ──────────────────────────────────────────────────────────

  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<SessionRow | null>(null);

  // ── Snackbar ────────────────────────────────────────────────────────────────

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

  // ── Data fetching ───────────────────────────────────────────────────────────

  const fetchUsers = useCallback(async () => {
    try {
      const result = await listUsers({ limit: 200 });
      setUsers(
        (result.data as { id: string; external_id: string }[]).map((u) => ({
          id: u.id,
          external_id: u.external_id,
        })),
      );
      if ((result.data as { id: string }[]).length > 0 && !userId) {
        setUserId((result.data as { id: string }[])[0].id);
      }
    } catch {
      // Users list failure is non-fatal for session page
    }
  }, [userId]);

  const fetchSessions = useCallback(
    async (selectedUserId?: string) => {
      const uid = selectedUserId ?? userId;
      if (!uid) {
        setLoading(false);
        return;
      }
      setLoading(true);
      try {
        const result = await listSessions(uid, {
          limit: 50,
          include_closed: true,
        });
        setSessions(result.data as SessionRow[]);
        setCursor(result.next_cursor ?? null);
        setHasMore(result.has_more);
      } catch (err) {
        const message =
          err instanceof ApiError
            ? err.detail ?? "Failed to load sessions"
            : "An unexpected error occurred";
        showSnackbar(message, "error");
      } finally {
        setLoading(false);
      }
    },
    [userId, showSnackbar],
  );

  const loadMore = useCallback(async () => {
    if (loadingMore || !cursor || !userId) return;
    setLoadingMore(true);
    try {
      const result = await listSessions(userId, {
        limit: 50,
        cursor,
        include_closed: true,
      });
      setSessions((prev) => [...prev, ...(result.data as SessionRow[])]);
      setCursor(result.next_cursor ?? null);
      setHasMore(result.has_more);
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.detail ?? "Failed to load more sessions"
          : "An unexpected error occurred";
      showSnackbar(message, "error");
    } finally {
      setLoadingMore(false);
    }
  }, [loadingMore, cursor, userId, showSnackbar]);

  useEffect(() => {
    fetchUsers();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (userId) {
      fetchSessions(userId);
    }
  }, [userId]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Create session ─────────────────────────────────────────────────────────

  const handleOpenCreateDialog = () => {
    setExternalId("");
    setFormError("");
    setCreateDialogOpen(true);
  };

  const handleCreateSession = async () => {
    if (!externalId.trim()) {
      setFormError("External ID is required");
      return;
    }
    if (!userId) {
      setFormError("No user selected");
      return;
    }

    setSubmitting(true);
    try {
      await createSession(userId, {
        external_id: externalId.trim(),
        metadata: {},
      });
      showSnackbar("Session created successfully", "success");
      setCreateDialogOpen(false);
      await fetchSessions(userId);
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.detail ?? "Failed to create session"
          : "An unexpected error occurred";
      showSnackbar(message, "error");
    } finally {
      setSubmitting(false);
    }
  };

  // ── Delete session ─────────────────────────────────────────────────────────

  const handleOpenDeleteDialog = (session: SessionRow) => {
    setDeleteTarget(session);
    setDeleteDialogOpen(true);
  };

  const handleDeleteSession = async () => {
    if (!deleteTarget) return;
    setSubmitting(true);
    try {
      await deleteSession(deleteTarget.user_id, deleteTarget.id);
      showSnackbar("Session deleted successfully", "success");
      setDeleteDialogOpen(false);
      setDeleteTarget(null);
      if (userId) await fetchSessions(userId);
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.detail ?? "Failed to delete session"
          : "An unexpected error occurred";
      showSnackbar(message, "error");
    } finally {
      setSubmitting(false);
    }
  };

  // ── DataGrid columns ────────────────────────────────────────────────────────

  const columns: GridColDef<SessionRow>[] = [
    {
      field: "external_id",
      headerName: "External ID",
      flex: 1,
      minWidth: 160,
    },
    {
      field: "user_id",
      headerName: "User ID",
      flex: 1,
      minWidth: 200,
      renderCell: ({ value }) => (
        <Typography variant="body2" sx={{ fontFamily: "monospace", fontSize: "0.8rem" }}>
          {value}
        </Typography>
      ),
    },
    {
      field: "is_active",
      headerName: "Status",
      width: 100,
      renderCell: ({ value }) => (
        <Chip
          label={value ? "Active" : "Closed"}
          color={value ? "success" : "default"}
          size="small"
        />
      ),
    },
    {
      field: "message_count",
      headerName: "Messages",
      width: 100,
      align: "center",
    },
    {
      field: "fact_count",
      headerName: "Facts",
      width: 80,
      align: "center",
    },
    {
      field: "created_at",
      headerName: "Created",
      width: 120,
      valueGetter: (_value, row) => new Date(row.created_at).toLocaleDateString(),
    },
    {
      field: "actions",
      headerName: "Actions",
      width: 110,
      sortable: false,
      renderCell: ({ row }) => (
        <Box sx={{ display: "flex", alignItems: "center", gap: 0.5 }}>
          <Tooltip title="View session">
            <IconButton
              size="small"
              onClick={() => router.push(`/dashboard/sessions/${row.id}?userId=${row.user_id}`)}
            >
              <ViewIcon fontSize="small" />
            </IconButton>
          </Tooltip>
          <Tooltip title="Delete session">
            <IconButton
              size="small"
              onClick={() => handleOpenDeleteDialog(row)}
              sx={{ color: "error.main" }}
            >
              <DeleteIcon fontSize="small" />
            </IconButton>
          </Tooltip>
        </Box>
      ),
    },
  ];

  // ── Render ──────────────────────────────────────────────────────────────────

  return (
    <Box>
      {/* Header row */}
      <Box sx={{ display: "flex", justifyContent: "space-between", alignItems: "center", mb: 3 }}>
        <Typography variant="h5" sx={{ fontWeight: 600 }}>
          Sessions
        </Typography>
        <Box sx={{ display: "flex", gap: 2 }}>
          <TextField
            select
            size="small"
            value={userId}
            onChange={(e) => setUserId(e.target.value)}
            sx={{ minWidth: 200 }}
            slotProps={{
              select: { native: true },
            }}
          >
            <option value="">Select a user</option>
            {users.map((u) => (
              <option key={u.id} value={u.id}>
                {u.external_id}
              </option>
            ))}
          </TextField>
          <Button
            variant="contained"
            startIcon={<AddIcon />}
            onClick={handleOpenCreateDialog}
            disabled={!userId}
          >
            Create Session
          </Button>
        </Box>
      </Box>

      {/* Sessions table */}
      <Card>
        <DataGrid<SessionRow>
          rows={sessions}
          columns={columns}
          loading={loading}
          getRowId={(row) => row.id}
          getRowHeight={() => 52}
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

      {/* ── Create Session Dialog ──────────────────────────────────────────── */}
      <Dialog
        open={createDialogOpen}
        onClose={() => {
          if (!submitting) setCreateDialogOpen(false);
        }}
        maxWidth="sm"
        fullWidth
      >
        <DialogTitle>Create Session</DialogTitle>
        <DialogContent>
          <Box sx={{ display: "flex", flexDirection: "column", gap: 2, pt: 1 }}>
            <TextField
              label="External ID"
              required
              value={externalId}
              onChange={(e) => setExternalId(e.target.value)}
              error={!!formError}
              helperText={formError}
              slotProps={{ htmlInput: { maxLength: 255 } }}
            />
            <TextField
              label="User"
              value={users.find((u) => u.id === userId)?.external_id ?? userId}
              disabled
              helperText="Session is created for the currently selected user"
            />
          </Box>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setCreateDialogOpen(false)} disabled={submitting}>
            Cancel
          </Button>
          <Button variant="contained" onClick={handleCreateSession} disabled={submitting}>
            {submitting ? "Creating..." : "Create"}
          </Button>
        </DialogActions>
      </Dialog>

      {/* ── Delete Confirmation Dialog ─────────────────────────────────────── */}
      <Dialog
        open={deleteDialogOpen}
        onClose={() => {
          if (!submitting) {
            setDeleteDialogOpen(false);
            setDeleteTarget(null);
          }
        }}
        maxWidth="xs"
        fullWidth
      >
        <DialogTitle>Delete Session</DialogTitle>
        <DialogContent>
          <Typography variant="body1">
            Are you sure you want to delete session{" "}
            <Typography component="strong" sx={{ fontWeight: 600 }}>
              {deleteTarget?.external_id}
            </Typography>
            ? Messages will be unlinked but preserved.
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button
            onClick={() => {
              setDeleteDialogOpen(false);
              setDeleteTarget(null);
            }}
            disabled={submitting}
          >
            Cancel
          </Button>
          <Button
            variant="contained"
            color="error"
            onClick={handleDeleteSession}
            disabled={submitting}
          >
            {submitting ? "Deleting..." : "Delete"}
          </Button>
        </DialogActions>
      </Dialog>

      {/* ── Snackbar ───────────────────────────────────────────────────────── */}
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
