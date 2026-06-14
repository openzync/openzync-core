"use client";

import { useState, useCallback, useEffect } from "react";
import { useRouter } from "next/navigation";
import {
  Box,
  Button,
  Chip,
  TextField,
  Typography,
  IconButton,
  Tooltip,
  Autocomplete,
} from "@mui/material";
import {
  Add as AddIcon,
  Visibility as ViewIcon,
  Delete as DeleteIcon,
} from "@mui/icons-material";
import type { GridColDef } from "@mui/x-data-grid";
import {
  listSessions,
  createSession,
  deleteSession,
  listUsers,
  ApiError,
} from "@/lib/api/client";
import { useAuth } from "@/lib/auth/useAuth";
import { useNotification } from "@/components/shared/NotificationProvider";
import PageHeader from "@/components/shared/PageHeader";
import DataTable from "@/components/shared/DataTable";
import FormDialog from "@/components/shared/FormDialog";
import ConfirmDialog from "@/components/shared/ConfirmDialog";

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

interface UserOption {
  id: string;
  external_id: string;
}

// ─── Page Component ───────────────────────────────────────────────────────────

export default function SessionsPage() {
  useAuth();
  const router = useRouter();
  const { showNotification } = useNotification();

  // ── Data state ──────────────────────────────────────────────────────────────

  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);

  // ── User filter ────────────────────────────────────────────────────────────

  const [selectedUserId, setSelectedUserId] = useState("");
  const [users, setUsers] = useState<UserOption[]>([]);

  // ── Create dialog ──────────────────────────────────────────────────────────

  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [createUserId, setCreateUserId] = useState("");
  const [createExternalId, setCreateExternalId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState("");

  // ── Delete dialog ──────────────────────────────────────────────────────────

  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<SessionRow | null>(null);

  // ── Data fetching ───────────────────────────────────────────────────────────

  const fetchUsers = useCallback(async () => {
    try {
      const result = await listUsers({ limit: 200 });
      const userList: UserOption[] = (
        result.data as { id: string; external_id: string }[]
      ).map((u) => ({
        id: u.id,
        external_id: u.external_id,
      }));
      setUsers(userList);
      if (userList.length > 0 && !selectedUserId) {
        setSelectedUserId(userList[0].id);
      }
    } catch {
      // Users list failure is non-fatal for session page
    }
  }, [selectedUserId]);

  const fetchSessions = useCallback(
    async (uid?: string) => {
      const userId = uid ?? selectedUserId;
      if (!userId) {
        setLoading(false);
        return;
      }
      setLoading(true);
      try {
        const result = await listSessions(userId, {
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
        showNotification(message, "error");
      } finally {
        setLoading(false);
      }
    },
    [selectedUserId, showNotification],
  );

  const loadMore = useCallback(async () => {
    if (loadingMore || !cursor || !selectedUserId) return;
    setLoadingMore(true);
    try {
      const result = await listSessions(selectedUserId, {
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
      showNotification(message, "error");
    } finally {
      setLoadingMore(false);
    }
  }, [loadingMore, cursor, selectedUserId, showNotification]);

  useEffect(() => {
    fetchUsers();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (selectedUserId) {
      fetchSessions(selectedUserId);
    }
  }, [selectedUserId]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Create session ─────────────────────────────────────────────────────────

  const handleOpenCreateDialog = () => {
    setCreateExternalId("");
    setCreateUserId(selectedUserId);
    setFormError("");
    setCreateDialogOpen(true);
  };

  const handleCreateSession = async () => {
    if (!createExternalId.trim()) {
      setFormError("External ID is required");
      return;
    }
    if (!createUserId) {
      setFormError("No user selected");
      return;
    }

    setSubmitting(true);
    try {
      await createSession(createUserId, {
        external_id: createExternalId.trim(),
        metadata: {},
      });
      showNotification("Session created successfully", "success");
      setCreateDialogOpen(false);
      await fetchSessions(createUserId);
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.detail ?? "Failed to create session"
          : "An unexpected error occurred";
      showNotification(message, "error");
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
      showNotification("Session deleted successfully", "success");
      setDeleteDialogOpen(false);
      setDeleteTarget(null);
      if (selectedUserId) await fetchSessions(selectedUserId);
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.detail ?? "Failed to delete session"
          : "An unexpected error occurred";
      showNotification(message, "error");
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

  const selectedUser: UserOption | undefined = users.find((u) => u.id === selectedUserId);

  return (
    <Box>
      <PageHeader
        title="Sessions"
        action={
          <Box sx={{ display: "flex", gap: 2, alignItems: "center" }}>
            <Autocomplete
              options={users}
              getOptionLabel={(option) => option.external_id}
              value={selectedUser}
              onChange={(_event, newValue) => {
                setSelectedUserId(newValue?.id ?? "");
              }}
              renderInput={(params) => (
                <TextField
                  {...params}
                  placeholder="Select a user"
                  size="small"
                  sx={{ minWidth: 220 }}
                />
              )}
              size="small"
              sx={{ minWidth: 220 }}
              disableClearable
            />
            <Button
              variant="contained"
              startIcon={<AddIcon />}
              onClick={handleOpenCreateDialog}
              disabled={!selectedUserId}
            >
              Create Session
            </Button>
          </Box>
        }
      />

      <DataTable<SessionRow>
        rows={sessions}
        columns={columns}
        loading={loading}
        getRowId={(row) => row.id}
        emptyTitle="No sessions found"
        emptyDescription={
          selectedUserId
            ? "This user has no sessions yet. Create one to get started."
            : "Select a user from the filter above to view their sessions."
        }
        emptyAction={
          selectedUserId
            ? { label: "Create Session", onClick: handleOpenCreateDialog }
            : undefined
        }
        hasMore={hasMore}
        loadingMore={loadingMore}
        onLoadMore={loadMore}
      />

      {/* ── Create Session Dialog ──────────────────────────────────────────── */}
      <FormDialog
        open={createDialogOpen}
        onClose={() => {
          if (!submitting) {
            setCreateDialogOpen(false);
            setFormError("");
          }
        }}
        title="Create Session"
        onSubmit={handleCreateSession}
        submitting={submitting}
        submitLabel="Create"
      >
        <Box sx={{ display: "flex", flexDirection: "column", gap: 2, pt: 1 }}>
          <TextField
            label="External ID"
            required
            value={createExternalId}
            onChange={(e) => setCreateExternalId(e.target.value)}
            error={!!formError && !createExternalId.trim()}
            helperText={
              !!formError && !createExternalId.trim()
                ? formError
                : "A unique identifier for this session"
            }
            slotProps={{ htmlInput: { maxLength: 255 } }}
            fullWidth
          />
          <Autocomplete
            options={users}
            getOptionLabel={(option) => option.external_id}
            value={users.find((u) => u.id === createUserId)}
            onChange={(_event, newValue) => {
              setCreateUserId(newValue?.id ?? "");
            }}
            renderInput={(params) => (
              <TextField
                {...params}
                label="User"
                required
                error={!createUserId && !!formError}
                helperText={!createUserId && !!formError ? formError : undefined}
              />
            )}
            size="small"
            disableClearable
          />
        </Box>
      </FormDialog>

      {/* ── Delete Confirmation Dialog ─────────────────────────────────────── */}
      <ConfirmDialog
        open={deleteDialogOpen}
        onClose={() => {
          if (!submitting) {
            setDeleteDialogOpen(false);
            setDeleteTarget(null);
          }
        }}
        onConfirm={handleDeleteSession}
        title="Delete Session"
        message={
          <>
            Are you sure you want to delete session{" "}
            <Typography component="span" sx={{ fontWeight: 600 }}>
              {deleteTarget?.external_id}
            </Typography>
            ? Messages will be unlinked but preserved.
          </>
        }
        confirmLabel="Delete"
        confirmColor="error"
        submitting={submitting}
      />
    </Box>
  );
}
