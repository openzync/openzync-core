"use client";

import { useState, useCallback, useEffect } from "react";
import {
  Box,
  Button,
  Chip,
  TextField,
  Typography,
  IconButton,
  Tooltip,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
} from "@mui/material";
import type { GridColDef } from "@mui/x-data-grid";
import {
  Add as AddIcon,
  ContentCopy as ContentCopyIcon,
  Block as BlockIcon,
} from "@mui/icons-material";
import {
  listApiKeys,
  createApiKey,
  revokeApiKey,
  ApiError,
} from "@/lib/api/client";
import { useAuth } from "@/lib/auth/useAuth";
import PageHeader from "@/components/shared/PageHeader";
import DataTable from "@/components/shared/DataTable";
import FormDialog from "@/components/shared/FormDialog";
import ConfirmDialog from "@/components/shared/ConfirmDialog";
import { useNotification } from "@/components/shared/NotificationProvider";

// ─── Types ────────────────────────────────────────────────────────────────────

interface ApiKeyRow {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  is_revoked: boolean;
  last_used_at: string | null;
  created_at: string;
}

interface CreatedKeyInfo {
  id: string;
  name: string;
  raw_key: string;
  message: string;
  prefix: string;
  scopes: string[];
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function formatDate(raw: string | null): string {
  if (!raw) return "Never";
  try {
    return new Date(raw).toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  } catch {
    return "—";
  }
}

// ─── Page Component ───────────────────────────────────────────────────────────

export default function ApiKeysPage() {
  useAuth();
  const { showNotification } = useNotification();

  // ── Data state ──────────────────────────────────────────────────────────────

  const [keys, setKeys] = useState<ApiKeyRow[]>([]);
  const [loading, setLoading] = useState(true);

  // ── Dialog state ────────────────────────────────────────────────────────────

  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [createdKey, setCreatedKey] = useState<CreatedKeyInfo | null>(null);
  const [revokeDialogOpen, setRevokeDialogOpen] = useState(false);
  const [revokeTarget, setRevokeTarget] = useState<ApiKeyRow | null>(null);

  // ── Form state ──────────────────────────────────────────────────────────────

  const [keyName, setKeyName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState("");

  // ── Data fetching ───────────────────────────────────────────────────────────

  const fetchKeys = useCallback(async () => {
    setLoading(true);
    try {
      const result = await listApiKeys();
      setKeys(result.data as ApiKeyRow[]);
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.detail ?? "Failed to load API keys"
          : "An unexpected error occurred";
      showNotification(message, "error");
    } finally {
      setLoading(false);
    }
  }, [showNotification]);

  useEffect(() => {
    fetchKeys();
  }, [fetchKeys]);

  // ── Create key ──────────────────────────────────────────────────────────────

  const handleOpenCreateDialog = () => {
    setKeyName("");
    setFormError("");
    setCreateDialogOpen(true);
  };

  const handleCreateKey = async () => {
    const trimmed = keyName.trim();
    if (!trimmed) {
      setFormError("Key name is required");
      return;
    }

    setSubmitting(true);
    try {
      const result = await createApiKey({ name: trimmed });
      // Cast to CreatedKeyInfo — raw_key is populated on creation response.
      const created = result as unknown as CreatedKeyInfo;
      setCreatedKey(created);
      setCreateDialogOpen(false);
      setKeyName("");
      await fetchKeys();
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.detail ?? "Failed to create API key"
          : "An unexpected error occurred";
      showNotification(message, "error");
    } finally {
      setSubmitting(false);
    }
  };

  // ── Copy raw key ────────────────────────────────────────────────────────────

  const handleCopyRawKey = useCallback(
    async (rawKey: string) => {
      try {
        await navigator.clipboard.writeText(rawKey);
        showNotification("API key copied to clipboard", "success");
      } catch {
        showNotification("Failed to copy key", "error");
      }
    },
    [showNotification],
  );

  // ── Revoke key ──────────────────────────────────────────────────────────────

  const handleOpenRevokeDialog = (key: ApiKeyRow) => {
    setRevokeTarget(key);
    setRevokeDialogOpen(true);
  };

  const handleRevokeKey = async () => {
    if (!revokeTarget) return;

    setSubmitting(true);
    try {
      await revokeApiKey(revokeTarget.id);
      showNotification("API key revoked successfully", "success");
      setRevokeDialogOpen(false);
      setRevokeTarget(null);
      await fetchKeys();
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.detail ?? "Failed to revoke API key"
          : "An unexpected error occurred";
      showNotification(message, "error");
    } finally {
      setSubmitting(false);
    }
  };

  // ── DataTable columns ───────────────────────────────────────────────────────

  const columns: GridColDef<ApiKeyRow>[] = [
    {
      field: "name",
      headerName: "Name",
      flex: 1,
      minWidth: 160,
    },
    {
      field: "prefix",
      headerName: "Prefix",
      width: 140,
      renderCell: ({ value }) => (
        <Chip
          label={value}
          size="small"
          sx={{ fontFamily: "monospace", letterSpacing: 0.5 }}
        />
      ),
    },
    {
      field: "scopes",
      headerName: "Scopes",
      flex: 1,
      minWidth: 180,
      sortable: false,
      renderCell: ({ value }) => (
        <Box sx={{ display: "flex", gap: 0.5, flexWrap: "wrap" }}>
          {Array.isArray(value) && value.length > 0
            ? value.map((scope) => (
                <Chip
                  key={scope}
                  label={scope}
                  size="small"
                  variant="outlined"
                />
              ))
            : (
              <Typography variant="body2" sx={{ color: "text.secondary" }}>
                —
              </Typography>
            )}
        </Box>
      ),
    },
    {
      field: "last_used_at",
      headerName: "Last Used",
      width: 130,
      valueGetter: (_value, row) => formatDate(row.last_used_at),
    },
    {
      field: "created_at",
      headerName: "Created",
      width: 130,
      valueGetter: (_value, row) => formatDate(row.created_at),
    },
    {
      field: "actions",
      headerName: "Actions",
      width: 100,
      sortable: false,
      renderCell: ({ row }) => (
        <Box sx={{ display: "flex", alignItems: "center" }}>
          <Tooltip title={row.is_revoked ? "Already revoked" : "Revoke key"}>
            <span>
              <IconButton
                size="small"
                disabled={row.is_revoked}
                onClick={() => handleOpenRevokeDialog(row)}
                sx={{ color: "error.main" }}
              >
                <BlockIcon fontSize="small" />
              </IconButton>
            </span>
          </Tooltip>
        </Box>
      ),
    },
  ];

  // ── Render ──────────────────────────────────────────────────────────────────

  return (
    <Box>
      <PageHeader
        title="API Keys"
        action={
          <Button
            variant="contained"
            startIcon={<AddIcon />}
            onClick={handleOpenCreateDialog}
          >
            Create Key
          </Button>
        }
      />

      {/* Keys table */}
      <DataTable<ApiKeyRow>
        rows={keys}
        columns={columns}
        loading={loading}
        getRowId={(row) => row.id}
        emptyTitle="No API keys found"
        emptyDescription="Create your first API key to authenticate requests."
        emptyAction={{ label: "Create Key", onClick: handleOpenCreateDialog }}
      />

      {/* ── Create Key Dialog ─────────────────────────────────────────────── */}
      <FormDialog
        open={createDialogOpen}
        onClose={() => setCreateDialogOpen(false)}
        title="Create API Key"
        onSubmit={handleCreateKey}
        submitting={submitting}
        submitLabel="Create"
      >
        <Box sx={{ display: "flex", flexDirection: "column", gap: 2, pt: 1 }}>
          <TextField
            label="Key Name"
            placeholder="e.g. Production CI/CD"
            required
            value={keyName}
            onChange={(e) => {
              setKeyName(e.target.value);
              if (formError) setFormError("");
            }}
            error={!!formError}
            helperText={formError}
            slotProps={{ htmlInput: { maxLength: 255 } }}
          />
        </Box>
      </FormDialog>

      {/* ── Key Created Dialog (raw key shown once) ───────────────────────── */}
      <Dialog
        open={createdKey !== null}
        onClose={() => setCreatedKey(null)}
        maxWidth="sm"
        fullWidth
      >
        <DialogTitle sx={{ fontWeight: 600, pb: 1 }}>
          API Key Created
        </DialogTitle>
        <DialogContent>
          <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
            <Typography
              variant="body2"
              sx={{ color: "warning.main", fontWeight: 600 }}
            >
              {createdKey?.message ??
                "Save this API key — it will not be shown again."}
            </Typography>

            {/* Dark terminal-style box for the raw key */}
            <Box
              sx={{
                position: "relative",
                bgcolor: "#0d1117",
                border: "1px solid",
                borderColor: "#30363d",
                borderRadius: 2,
                p: 2.5,
                pr: 7,
                fontFamily: "monospace",
              }}
            >
              <Typography
                variant="body2"
                sx={{
                  fontFamily: "inherit",
                  fontSize: "0.8125rem",
                  lineHeight: 1.6,
                  wordBreak: "break-all",
                  color: "#e6edf3",
                  letterSpacing: 0.3,
                }}
              >
                {createdKey?.raw_key}
              </Typography>
              <IconButton
                size="small"
                onClick={() =>
                  createdKey && handleCopyRawKey(createdKey.raw_key)
                }
                sx={{
                  position: "absolute",
                  top: 8,
                  right: 8,
                  color: "#8b949e",
                  bgcolor: "rgba(255,255,255,0.05)",
                  "&:hover": {
                    color: "#e6edf3",
                    bgcolor: "rgba(255,255,255,0.1)",
                  },
                }}
              >
                <ContentCopyIcon fontSize="small" />
              </IconButton>
            </Box>

            <Typography variant="caption" sx={{ color: "text.secondary" }}>
              You can use this key to authenticate API requests. Store it
              securely.
            </Typography>
          </Box>
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button variant="contained" onClick={() => setCreatedKey(null)}>
            Done
          </Button>
        </DialogActions>
      </Dialog>

      {/* ── Revoke Confirmation Dialog ────────────────────────────────────── */}
      <ConfirmDialog
        open={revokeDialogOpen}
        onClose={() => {
          setRevokeDialogOpen(false);
          setRevokeTarget(null);
        }}
        onConfirm={handleRevokeKey}
        title="Revoke API Key"
        message={
          <>
            Are you sure you want to revoke key{" "}
            <Typography component="strong" sx={{ fontWeight: 600 }}>
              {revokeTarget?.name}
            </Typography>
            ? Any services using this key will immediately lose access.
          </>
        }
        confirmLabel="Revoke"
        confirmColor="error"
        submitting={submitting}
      />
    </Box>
  );
}
