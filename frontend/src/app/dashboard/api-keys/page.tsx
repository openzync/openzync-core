"use client";

import { useState, useCallback, useEffect } from "react";
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
  ContentCopy as ContentCopyIcon,
  Block as BlockIcon,
} from "@mui/icons-material";
import { listApiKeys, createApiKey, revokeApiKey, ApiError } from "@/lib/api/client";
import { useAuth } from "@/lib/auth/useAuth";

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

interface SnackbarState {
  open: boolean;
  message: string;
  severity: "success" | "error";
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
      showSnackbar(message, "error");
    } finally {
      setLoading(false);
    }
  }, [showSnackbar]);

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
      showSnackbar(message, "error");
    } finally {
      setSubmitting(false);
    }
  };

  // ── Copy raw key ────────────────────────────────────────────────────────────

  const handleCopyRawKey = useCallback(
    async (rawKey: string) => {
      try {
        await navigator.clipboard.writeText(rawKey);
        showSnackbar("API key copied to clipboard", "success");
      } catch {
        showSnackbar("Failed to copy key", "error");
      }
    },
    [showSnackbar],
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
      showSnackbar("API key revoked successfully", "success");
      setRevokeDialogOpen(false);
      setRevokeTarget(null);
      await fetchKeys();
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.detail ?? "Failed to revoke API key"
          : "An unexpected error occurred";
      showSnackbar(message, "error");
    } finally {
      setSubmitting(false);
    }
  };

  // ── DataGrid columns ────────────────────────────────────────────────────────

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
        <Chip label={value} size="small" sx={{ fontFamily: "monospace", letterSpacing: 0.5 }} />
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
                <Chip key={scope} label={scope} size="small" variant="outlined" />
              ))
            : <Typography variant="body2" sx={{ color: "text.secondary" }}>—</Typography>}
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
      {/* Header row */}
      <Box sx={{ display: "flex", justifyContent: "space-between", alignItems: "center", mb: 3 }}>
        <Typography variant="h5" sx={{ fontWeight: 600 }}>
          API Keys
        </Typography>
        <Button variant="contained" startIcon={<AddIcon />} onClick={handleOpenCreateDialog}>
          Create Key
        </Button>
      </Box>

      {/* Keys table */}
      <Card>
        <DataGrid<ApiKeyRow>
          rows={keys}
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
      </Card>

      {/* ── Create Key Dialog ──────────────────────────────────────────────── */}
      <Dialog
        open={createDialogOpen}
        onClose={() => {
          if (!submitting) setCreateDialogOpen(false);
        }}
        maxWidth="sm"
        fullWidth
      >
        <DialogTitle>Create API Key</DialogTitle>
        <DialogContent>
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
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setCreateDialogOpen(false)} disabled={submitting}>
            Cancel
          </Button>
          <Button variant="contained" onClick={handleCreateKey} disabled={submitting}>
            {submitting ? "Creating..." : "Create"}
          </Button>
        </DialogActions>
      </Dialog>

      {/* ── Key Created Dialog (raw key shown once) ────────────────────────── */}
      <Dialog
        open={createdKey !== null}
        onClose={() => setCreatedKey(null)}
        maxWidth="sm"
        fullWidth
      >
        <DialogTitle>API Key Created</DialogTitle>
        <DialogContent>
          <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
            <Typography variant="body2" sx={{ color: "warning.main", fontWeight: 600 }}>
              {createdKey?.message ?? "Save this API key — it will not be shown again."}
            </Typography>
            <Box
              sx={{
                position: "relative",
                backgroundColor: "grey.900",
                borderRadius: 1,
                p: 2,
                pr: 6,
              }}
            >
              <Typography
                variant="body2"
                sx={{
                  fontFamily: "monospace",
                  fontSize: "0.8125rem",
                  wordBreak: "break-all",
                  color: "common.white",
                }}
              >
                {createdKey?.raw_key}
              </Typography>
              <IconButton
                size="small"
                onClick={() => createdKey && handleCopyRawKey(createdKey.raw_key)}
                sx={{
                  position: "absolute",
                  top: 8,
                  right: 8,
                  color: "grey.400",
                  "&:hover": { color: "common.white" },
                }}
              >
                <ContentCopyIcon fontSize="small" />
              </IconButton>
            </Box>
            <Typography variant="caption" sx={{ color: "text.secondary" }}>
              You can use this key to authenticate API requests. Store it securely.
            </Typography>
          </Box>
        </DialogContent>
        <DialogActions>
          <Button variant="contained" onClick={() => setCreatedKey(null)}>
            Done
          </Button>
        </DialogActions>
      </Dialog>

      {/* ── Revoke Confirmation Dialog ─────────────────────────────────────── */}
      <Dialog
        open={revokeDialogOpen}
        onClose={() => {
          if (!submitting) {
            setRevokeDialogOpen(false);
            setRevokeTarget(null);
          }
        }}
        maxWidth="xs"
        fullWidth
      >
        <DialogTitle>Revoke API Key</DialogTitle>
        <DialogContent>
          <Typography variant="body1">
            Are you sure you want to revoke key{" "}
            <Typography component="strong" sx={{ fontWeight: 600 }}>
              {revokeTarget?.name}
            </Typography>
            ? Any services using this key will immediately lose access.
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button
            onClick={() => {
              setRevokeDialogOpen(false);
              setRevokeTarget(null);
            }}
            disabled={submitting}
          >
            Cancel
          </Button>
          <Button
            variant="contained"
            color="error"
            onClick={handleRevokeKey}
            disabled={submitting}
          >
            {submitting ? "Revoking..." : "Revoke"}
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
