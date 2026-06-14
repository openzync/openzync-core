"use client";

import { useState, useCallback, useEffect } from "react";
import {
  Box,
  Button,
  TextField,
  Typography,
  IconButton,
  Tooltip,
} from "@mui/material";
import type { GridColDef } from "@mui/x-data-grid";
import {
  Add as AddIcon,
  ContentCopy as ContentCopyIcon,
  Edit as EditIcon,
  Delete as DeleteIcon,
} from "@mui/icons-material";
import {
  listUsers,
  createUser,
  updateUser,
  deleteUser,
  ApiError,
} from "@/lib/api/client";
import { useAuth } from "@/lib/auth/useAuth";
import PageHeader from "@/components/shared/PageHeader";
import DataTable from "@/components/shared/DataTable";
import FormDialog from "@/components/shared/FormDialog";
import ConfirmDialog from "@/components/shared/ConfirmDialog";
import { useNotification } from "@/components/shared/NotificationProvider";

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

// ─── Page Component ───────────────────────────────────────────────────────────

export default function UsersPage() {
  // Auth is guarded by dashboard layout AuthGuard; hook available for
  // per-user RBAC decisions on future features (e.g. admin-only delete).
  useAuth();
  const { showNotification } = useNotification();

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
      showNotification(message, "error");
    } finally {
      setLoading(false);
    }
  }, [showNotification]);

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
      showNotification(message, "error");
    } finally {
      setLoadingMore(false);
    }
  }, [loadingMore, cursor, showNotification]);

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
      showNotification("User created successfully", "success");
      setCreateDialogOpen(false);
      await fetchUsers();
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.detail ?? "Failed to create user"
          : "An unexpected error occurred";
      showNotification(message, "error");
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
      showNotification("User updated successfully", "success");
      setEditDialogOpen(false);
      setEditTarget(null);
      await fetchUsers();
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.detail ?? "Failed to update user"
          : "An unexpected error occurred";
      showNotification(message, "error");
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
      showNotification("User deleted successfully", "success");
      setDeleteDialogOpen(false);
      setDeleteTarget(null);
      await fetchUsers();
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.detail ?? "Failed to delete user"
          : "An unexpected error occurred";
      showNotification(message, "error");
    } finally {
      setSubmitting(false);
    }
  };

  // ── Clipboard helper ────────────────────────────────────────────────────────

  const handleCopyId = useCallback(
    async (id: string) => {
      try {
        await navigator.clipboard.writeText(id);
        showNotification("User ID copied to clipboard", "success");
      } catch {
        showNotification("Failed to copy ID", "error");
      }
    },
    [showNotification],
  );

  // ── DataTable columns ───────────────────────────────────────────────────────

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
        <Typography
          variant="body2"
          sx={{ color: row.name ? "text.primary" : "text.secondary" }}
        >
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
        <Typography
          variant="body2"
          sx={{ color: row.email ? "text.primary" : "text.secondary" }}
        >
          {formattedValue}
        </Typography>
      ),
    },
    {
      field: "created_at",
      headerName: "Created",
      flex: 0.5,
      minWidth: 120,
      valueGetter: (_value, row) =>
        new Date(row.created_at).toLocaleDateString(),
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
            <IconButton
              size="small"
              onClick={() => handleOpenEditDialog(row)}
            >
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
      <PageHeader
        title="Users"
        action={
          <Button
            variant="contained"
            startIcon={<AddIcon />}
            onClick={handleOpenCreateDialog}
          >
            Create User
          </Button>
        }
      />

      {/* Users table */}
      <DataTable<UserRow>
        rows={users}
        columns={columns}
        loading={loading}
        getRowId={(row) => row.id}
        hasMore={hasMore}
        loadingMore={loadingMore}
        onLoadMore={loadMore}
        emptyTitle="No users found"
        emptyDescription="Create your first user to get started."
        emptyAction={{ label: "Create User", onClick: handleOpenCreateDialog }}
      />

      {/* ── Create User Dialog ────────────────────────────────────────────── */}
      <FormDialog
        open={createDialogOpen}
        onClose={() => setCreateDialogOpen(false)}
        title="Create User"
        onSubmit={handleCreateUser}
        submitting={submitting}
        submitLabel="Create"
      >
        <Box sx={{ display: "flex", flexDirection: "column", gap: 2, pt: 1 }}>
          <TextField
            label="External ID"
            required
            value={createForm.external_id}
            onChange={(e) =>
              setCreateForm((prev) => ({
                ...prev,
                external_id: e.target.value,
              }))
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
      </FormDialog>

      {/* ── Edit User Dialog ──────────────────────────────────────────────── */}
      <FormDialog
        open={editDialogOpen}
        onClose={() => {
          setEditDialogOpen(false);
          setEditTarget(null);
        }}
        title="Edit User"
        onSubmit={handleEditUser}
        submitting={submitting}
        submitLabel="Save"
      >
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
      </FormDialog>

      {/* ── Delete Confirmation Dialog ────────────────────────────────────── */}
      <ConfirmDialog
        open={deleteDialogOpen}
        onClose={() => {
          setDeleteDialogOpen(false);
          setDeleteTarget(null);
        }}
        onConfirm={handleDeleteUser}
        title="Delete User"
        message={
          <>
            Are you sure you want to delete user{" "}
            <Typography component="strong" sx={{ fontWeight: 600 }}>
              {deleteTarget?.external_id}
            </Typography>
            ? This action cannot be undone.
          </>
        }
        confirmLabel="Delete"
        confirmColor="error"
        submitting={submitting}
      />
    </Box>
  );
}
