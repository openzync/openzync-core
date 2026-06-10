"use client";

import { useState, useCallback, useEffect } from "react";
import {
  Box,
  Button,
  Card,
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
  ContentCopy as ContentCopyIcon,
  Edit as EditIcon,
  Delete as DeleteIcon,
} from "@mui/icons-material";
import { listUsers, createUser, updateUser, deleteUser, ApiError } from "@/lib/api/client";
import { useAuth } from "@/lib/auth/useAuth";

// ─── Types ────────────────────────────────────────────────────────────────────

interface UserRow {
  id: string;
  external_id: string;
  name: string | null;
  email: string | null;
  created_at: string;
  is_deleted: boolean;
}

interface CreateUserForm {
  external_id: string;
  name: string;
  email: string;
}

interface EditUserForm {
  external_id: string;
  name: string;
  email: string;
}

interface SnackbarState {
  open: boolean;
  message: string;
  severity: "success" | "error";
}

// ─── Page Component ───────────────────────────────────────────────────────────

export default function UsersPage() {
  // Auth is guarded by dashboard layout AuthGuard; hook available for
  // per-user RBAC decisions on future features (e.g. admin-only delete).
  useAuth();

  // ── Data state ──────────────────────────────────────────────────────────────

  const [users, setUsers] = useState<UserRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);

  // ── Dialog state ────────────────────────────────────────────────────────────

  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<UserRow | null>(null);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<UserRow | null>(null);

  // ── Form state ──────────────────────────────────────────────────────────────

  const [createForm, setCreateForm] = useState<CreateUserForm>({
    external_id: "",
    name: "",
    email: "",
  });
  const [editForm, setEditForm] = useState<EditUserForm>({
    external_id: "",
    name: "",
    email: "",
  });
  const [submitting, setSubmitting] = useState(false);
  const [formErrors, setFormErrors] = useState<Record<string, string>>({});

  // ── Snackbar state ──────────────────────────────────────────────────────────

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
    setLoading(true);
    try {
      const result = await listUsers({ limit: 50 });
      setUsers(result.data as UserRow[]);
      setCursor(result.next_cursor ?? null);
      setHasMore(result.has_more);
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.detail ?? "Failed to load users"
          : "An unexpected error occurred";
      showSnackbar(message, "error");
    } finally {
      setLoading(false);
    }
  }, [showSnackbar]);

  const loadMore = useCallback(async () => {
    if (loadingMore || !cursor) return;
    setLoadingMore(true);
    try {
      const result = await listUsers({ limit: 50, cursor });
      setUsers((prev) => [...prev, ...(result.data as UserRow[])]);
      setCursor(result.next_cursor ?? null);
      setHasMore(result.has_more);
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.detail ?? "Failed to load more users"
          : "An unexpected error occurred";
      showSnackbar(message, "error");
    } finally {
      setLoadingMore(false);
    }
  }, [loadingMore, cursor, showSnackbar]);

  useEffect(() => {
    fetchUsers();
  }, [fetchUsers]);

  // ── Create user ─────────────────────────────────────────────────────────────

  const handleOpenCreateDialog = () => {
    setCreateForm({ external_id: "", name: "", email: "" });
    setFormErrors({});
    setCreateDialogOpen(true);
  };

  const handleCreateUser = async () => {
    const errors: Record<string, string> = {};
    if (!createForm.external_id.trim()) {
      errors.external_id = "External ID is required";
    }
    if (Object.keys(errors).length > 0) {
      setFormErrors(errors);
      return;
    }

    setSubmitting(true);
    try {
      await createUser({
        external_id: createForm.external_id.trim(),
        name: createForm.name.trim() || undefined,
        email: createForm.email.trim() || undefined,
      });
      showSnackbar("User created successfully", "success");
      setCreateDialogOpen(false);
      await fetchUsers();
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.detail ?? "Failed to create user"
          : "An unexpected error occurred";
      showSnackbar(message, "error");
    } finally {
      setSubmitting(false);
    }
  };

  // ── Edit user ───────────────────────────────────────────────────────────────

  const handleOpenEditDialog = (user: UserRow) => {
    setEditTarget(user);
    setEditForm({
      external_id: user.external_id,
      name: user.name ?? "",
      email: user.email ?? "",
    });
    setFormErrors({});
    setEditDialogOpen(true);
  };

  const handleEditUser = async () => {
    if (!editTarget) return;

    setSubmitting(true);
    try {
      await updateUser(editTarget.id, {
        name: editForm.name.trim() || undefined,
        email: editForm.email.trim() || undefined,
      });
      showSnackbar("User updated successfully", "success");
      setEditDialogOpen(false);
      setEditTarget(null);
      await fetchUsers();
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.detail ?? "Failed to update user"
          : "An unexpected error occurred";
      showSnackbar(message, "error");
    } finally {
      setSubmitting(false);
    }
  };

  // ── Delete user ─────────────────────────────────────────────────────────────

  const handleOpenDeleteDialog = (user: UserRow) => {
    setDeleteTarget(user);
    setDeleteDialogOpen(true);
  };

  const handleDeleteUser = async () => {
    if (!deleteTarget) return;

    setSubmitting(true);
    try {
      await deleteUser(deleteTarget.id);
      showSnackbar("User deleted successfully", "success");
      setDeleteDialogOpen(false);
      setDeleteTarget(null);
      await fetchUsers();
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.detail ?? "Failed to delete user"
          : "An unexpected error occurred";
      showSnackbar(message, "error");
    } finally {
      setSubmitting(false);
    }
  };

  // ── Clipboard helper ────────────────────────────────────────────────────────

  const handleCopyId = useCallback(
    async (id: string) => {
      try {
        await navigator.clipboard.writeText(id);
        showSnackbar("User ID copied to clipboard", "success");
      } catch {
        showSnackbar("Failed to copy ID", "error");
      }
    },
    [showSnackbar],
  );

  // ── DataGrid columns ────────────────────────────────────────────────────────

  const columns: GridColDef<UserRow>[] = [
    {
      field: "external_id",
      headerName: "External ID",
      flex: 1,
      minWidth: 150,
    },
    {
      field: "name",
      headerName: "Name",
      flex: 1,
      minWidth: 150,
      valueGetter: (_value, row) => row.name ?? "—",
      renderCell: ({ row, formattedValue }) => (
        <Typography variant="body2" sx={{ color: row.name ? "text.primary" : "text.secondary" }}>
          {formattedValue}
        </Typography>
      ),
    },
    {
      field: "email",
      headerName: "Email",
      flex: 1,
      minWidth: 200,
      valueGetter: (_value, row) => row.email ?? "—",
      renderCell: ({ row, formattedValue }) => (
        <Typography variant="body2" sx={{ color: row.email ? "text.primary" : "text.secondary" }}>
          {formattedValue}
        </Typography>
      ),
    },
    {
      field: "created_at",
      headerName: "Created",
      flex: 0.5,
      minWidth: 120,
      valueGetter: (_value, row) => new Date(row.created_at).toLocaleDateString(),
    },
    {
      field: "actions",
      headerName: "Actions",
      width: 140,
      sortable: false,
      renderCell: ({ row }) => (
        <Box sx={{ display: "flex", alignItems: "center", gap: 0.5 }}>
          <Tooltip title="Copy ID">
            <IconButton size="small" onClick={() => handleCopyId(row.id)}>
              <ContentCopyIcon fontSize="small" />
            </IconButton>
          </Tooltip>
          <Tooltip title="Edit">
            <IconButton size="small" onClick={() => handleOpenEditDialog(row)}>
              <EditIcon fontSize="small" />
            </IconButton>
          </Tooltip>
          <Tooltip title="Delete">
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
          Users
        </Typography>
        <Button variant="contained" startIcon={<AddIcon />} onClick={handleOpenCreateDialog}>
          Create User
        </Button>
      </Box>

      {/* Users table */}
      <Card>
        <DataGrid<UserRow>
          rows={users}
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

      {/* ── Create User Dialog ─────────────────────────────────────────────── */}
      <Dialog
        open={createDialogOpen}
        onClose={() => {
          if (!submitting) setCreateDialogOpen(false);
        }}
        maxWidth="sm"
        fullWidth
      >
        <DialogTitle>Create User</DialogTitle>
        <DialogContent>
          <Box sx={{ display: "flex", flexDirection: "column", gap: 2, pt: 1 }}>
            <TextField
              label="External ID"
              required
              value={createForm.external_id}
              onChange={(e) =>
                setCreateForm((prev) => ({ ...prev, external_id: e.target.value }))
              }
              error={!!formErrors.external_id}
              helperText={formErrors.external_id}
              slotProps={{ htmlInput: { maxLength: 255 } }}
            />
            <TextField
              label="Name"
              value={createForm.name}
              onChange={(e) =>
                setCreateForm((prev) => ({ ...prev, name: e.target.value }))
              }
              slotProps={{ htmlInput: { maxLength: 255 } }}
            />
            <TextField
              label="Email"
              type="email"
              value={createForm.email}
              onChange={(e) =>
                setCreateForm((prev) => ({ ...prev, email: e.target.value }))
              }
              slotProps={{ htmlInput: { maxLength: 255 } }}
            />
          </Box>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setCreateDialogOpen(false)} disabled={submitting}>
            Cancel
          </Button>
          <Button variant="contained" onClick={handleCreateUser} disabled={submitting}>
            {submitting ? "Creating..." : "Create"}
          </Button>
        </DialogActions>
      </Dialog>

      {/* ── Edit User Dialog ───────────────────────────────────────────────── */}
      <Dialog
        open={editDialogOpen}
        onClose={() => {
          if (!submitting) {
            setEditDialogOpen(false);
            setEditTarget(null);
          }
        }}
        maxWidth="sm"
        fullWidth
      >
        <DialogTitle>Edit User</DialogTitle>
        <DialogContent>
          <Box sx={{ display: "flex", flexDirection: "column", gap: 2, pt: 1 }}>
            <TextField
              label="External ID"
              value={editForm.external_id}
              disabled
              helperText="External ID cannot be changed after creation"
            />
            <TextField
              label="Name"
              value={editForm.name}
              onChange={(e) =>
                setEditForm((prev) => ({ ...prev, name: e.target.value }))
              }
              slotProps={{ htmlInput: { maxLength: 255 } }}
            />
            <TextField
              label="Email"
              type="email"
              value={editForm.email}
              onChange={(e) =>
                setEditForm((prev) => ({ ...prev, email: e.target.value }))
              }
              slotProps={{ htmlInput: { maxLength: 255 } }}
            />
          </Box>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => { setEditDialogOpen(false); setEditTarget(null); }} disabled={submitting}>
            Cancel
          </Button>
          <Button variant="contained" onClick={handleEditUser} disabled={submitting}>
            {submitting ? "Saving..." : "Save"}
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
        <DialogTitle>Delete User</DialogTitle>
        <DialogContent>
          <Typography variant="body1">
            Are you sure you want to delete user{" "}
            <Typography component="strong" sx={{ fontWeight: 600 }}>
              {deleteTarget?.external_id}
            </Typography>
            ? This action cannot be undone.
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
            onClick={handleDeleteUser}
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
