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
  Select,
  MenuItem,
  InputLabel,
  FormControl,
  FormHelperText,
} from "@mui/material";
import { DataGrid, type GridColDef } from "@mui/x-data-grid";
import {
  Add as AddIcon,
  Visibility as VisibilityIcon,
  Delete as DeleteIcon,
} from "@mui/icons-material";
import { api, ApiError } from "@/lib/api/client";
import { useAuth } from "@/lib/auth/useAuth";

// ─── Types ────────────────────────────────────────────────────────────────────

interface SchemaRow {
  id: string;
  name: string;
  type: string;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

interface SchemaDetail extends SchemaRow {
  organization_id: string;
  json_schema: Record<string, unknown>;
  prompt_template: string | null;
}

interface CreateSchemaForm {
  name: string;
  type: string;
  json_schema: string;
  prompt_template: string;
}

interface SnackbarState {
  open: boolean;
  message: string;
  severity: "success" | "error";
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
    return "—";
  }
}

// ─── Page Component ───────────────────────────────────────────────────────────

export default function SchemasPage() {
  useAuth();

  // ── Data state ──────────────────────────────────────────────────────────────

  const [schemas, setSchemas] = useState<SchemaRow[]>([]);
  const [loading, setLoading] = useState(true);

  // ── Dialog state ────────────────────────────────────────────────────────────

  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [viewDialogOpen, setViewDialogOpen] = useState(false);
  const [viewTarget, setViewTarget] = useState<SchemaDetail | null>(null);
  const [viewLoading, setViewLoading] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<SchemaRow | null>(null);

  // ── Form state ──────────────────────────────────────────────────────────────

  const [createForm, setCreateForm] = useState<CreateSchemaForm>({
    name: "",
    type: "structured",
    json_schema: "",
    prompt_template: "",
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

  const fetchSchemas = useCallback(async () => {
    setLoading(true);
    try {
      const result = await api.get<{ data: SchemaRow[]; total: number }>(
        "/v1/admin/schemas",
      );
      setSchemas(result.data);
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.detail ?? "Failed to load schemas"
          : "An unexpected error occurred";
      showSnackbar(message, "error");
    } finally {
      setLoading(false);
    }
  }, [showSnackbar]);

  useEffect(() => {
    fetchSchemas();
  }, [fetchSchemas]);

  // ── View schema ─────────────────────────────────────────────────────────────

  const handleViewSchema = useCallback(
    async (schema: SchemaRow) => {
      setViewLoading(true);
      setViewDialogOpen(true);
      try {
        const detail = await api.get<SchemaDetail>(
          `/v1/admin/schemas/${schema.id}`,
        );
        setViewTarget(detail);
      } catch (err) {
        const message =
          err instanceof ApiError
            ? err.detail ?? "Failed to load schema details"
            : "An unexpected error occurred";
        showSnackbar(message, "error");
        setViewDialogOpen(false);
      } finally {
        setViewLoading(false);
      }
    },
    [showSnackbar],
  );

  // ── Create schema ───────────────────────────────────────────────────────────

  const handleOpenCreateDialog = () => {
    setCreateForm({
      name: "",
      type: "structured",
      json_schema: "",
      prompt_template: "",
    });
    setFormErrors({});
    setCreateDialogOpen(true);
  };

  const handleCreateSchema = async () => {
    const errors: Record<string, string> = {};
    const trimmedName = createForm.name.trim();
    if (!trimmedName) {
      errors.name = "Schema name is required";
    }

    const trimmedJson = createForm.json_schema.trim();
    if (!trimmedJson) {
      errors.json_schema = "JSON Schema is required";
    }

    let parsedJson: Record<string, unknown> | undefined;
    if (trimmedJson) {
      try {
        parsedJson = JSON.parse(trimmedJson) as Record<string, unknown>;
      } catch {
        errors.json_schema = "Invalid JSON format";
      }
    }

    if (Object.keys(errors).length > 0) {
      setFormErrors(errors);
      return;
    }

    setSubmitting(true);
    try {
      await api.post("/v1/admin/schemas", {
        name: trimmedName,
        type: createForm.type,
        json_schema: parsedJson,
        prompt_template:
          createForm.prompt_template.trim() || null,
      });
      showSnackbar("Schema created successfully", "success");
      setCreateDialogOpen(false);
      await fetchSchemas();
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.detail ?? "Failed to create schema"
          : "An unexpected error occurred";
      showSnackbar(message, "error");
    } finally {
      setSubmitting(false);
    }
  };

  // ── Delete schema ───────────────────────────────────────────────────────────

  const handleOpenDeleteDialog = (schema: SchemaRow) => {
    setDeleteTarget(schema);
    setDeleteDialogOpen(true);
  };

  const handleDeleteSchema = async () => {
    if (!deleteTarget) return;

    setSubmitting(true);
    try {
      await api.delete(`/v1/admin/schemas/${deleteTarget.id}`);
      showSnackbar("Schema deleted successfully", "success");
      setDeleteDialogOpen(false);
      setDeleteTarget(null);
      await fetchSchemas();
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.detail ?? "Failed to delete schema"
          : "An unexpected error occurred";
      showSnackbar(message, "error");
    } finally {
      setSubmitting(false);
    }
  };

  // ── DataGrid columns ────────────────────────────────────────────────────────

  const columns: GridColDef<SchemaRow>[] = [
    {
      field: "name",
      headerName: "Name",
      flex: 1,
      minWidth: 160,
    },
    {
      field: "type",
      headerName: "Type",
      width: 160,
      renderCell: ({ value }) => (
        <Chip
          label={value}
          size="small"
          variant="outlined"
          color={value === "structured" ? "primary" : "secondary"}
        />
      ),
    },
    {
      field: "is_active",
      headerName: "Active",
      width: 100,
      renderCell: ({ value }) => (
        <Chip
          label={value ? "Active" : "Inactive"}
          size="small"
          color={value ? "success" : "default"}
        />
      ),
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
      width: 120,
      sortable: false,
      renderCell: ({ row }) => (
        <Box sx={{ display: "flex", alignItems: "center", gap: 0.5 }}>
          <Tooltip title="View details">
            <IconButton size="small" onClick={() => handleViewSchema(row)}>
              <VisibilityIcon fontSize="small" />
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
      <Box
        sx={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          mb: 3,
        }}
      >
        <Typography variant="h5" sx={{ fontWeight: 600 }}>
          Extraction Schemas
        </Typography>
        <Button
          variant="contained"
          startIcon={<AddIcon />}
          onClick={handleOpenCreateDialog}
        >
          Create Schema
        </Button>
      </Box>

      {/* Schemas table */}
      <Card>
        <DataGrid<SchemaRow>
          rows={schemas}
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

      {/* ── Create Schema Dialog ──────────────────────────────────────────── */}
      <Dialog
        open={createDialogOpen}
        onClose={() => {
          if (!submitting) setCreateDialogOpen(false);
        }}
        maxWidth="md"
        fullWidth
      >
        <DialogTitle>Create Extraction Schema</DialogTitle>
        <DialogContent>
          <Box sx={{ display: "flex", flexDirection: "column", gap: 2, pt: 1 }}>
            <TextField
              label="Name"
              placeholder="e.g. invoice_extraction"
              required
              value={createForm.name}
              onChange={(e) =>
                setCreateForm((prev) => ({ ...prev, name: e.target.value }))
              }
              error={!!formErrors.name}
              helperText={formErrors.name}
              slotProps={{ htmlInput: { maxLength: 255 } }}
            />

            <FormControl error={!!formErrors.type}>
              <InputLabel id="schema-type-label">Type</InputLabel>
              <Select
                labelId="schema-type-label"
                label="Type"
                value={createForm.type}
                onChange={(e) =>
                  setCreateForm((prev) => ({ ...prev, type: e.target.value }))
                }
              >
                <MenuItem value="structured">structured</MenuItem>
                <MenuItem value="classification">classification</MenuItem>
              </Select>
              {formErrors.type && (
                <FormHelperText>{formErrors.type}</FormHelperText>
              )}
            </FormControl>

            <TextField
              label="JSON Schema"
              placeholder='{"type": "object", "properties": {...}}'
              required
              multiline
              minRows={6}
              maxRows={16}
              value={createForm.json_schema}
              onChange={(e) =>
                setCreateForm((prev) => ({
                  ...prev,
                  json_schema: e.target.value,
                }))
              }
              error={!!formErrors.json_schema}
              helperText={formErrors.json_schema}
              slotProps={{
                htmlInput: {
                  sx: { fontFamily: "monospace", fontSize: "0.8125rem" },
                },
              }}
            />

            <TextField
              label="Prompt Template (optional)"
              placeholder="Override prompt template for this schema..."
              multiline
              minRows={3}
              maxRows={8}
              value={createForm.prompt_template}
              onChange={(e) =>
                setCreateForm((prev) => ({
                  ...prev,
                  prompt_template: e.target.value,
                }))
              }
              slotProps={{
                htmlInput: {
                  sx: { fontFamily: "monospace", fontSize: "0.8125rem" },
                },
              }}
            />
          </Box>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setCreateDialogOpen(false)} disabled={submitting}>
            Cancel
          </Button>
          <Button variant="contained" onClick={handleCreateSchema} disabled={submitting}>
            {submitting ? "Creating..." : "Create"}
          </Button>
        </DialogActions>
      </Dialog>

      {/* ── View Schema Dialog ────────────────────────────────────────────── */}
      <Dialog
        open={viewDialogOpen}
        onClose={() => {
          if (!viewLoading) {
            setViewDialogOpen(false);
            setViewTarget(null);
          }
        }}
        maxWidth="md"
        fullWidth
      >
        <DialogTitle>
          {viewTarget ? (
            <Typography variant="h6" sx={{ fontWeight: 600 }}>
              {viewTarget.name}
            </Typography>
          ) : (
            "Schema Details"
          )}
        </DialogTitle>
        <DialogContent>
          {viewLoading && (
            <Typography variant="body2" sx={{ color: "text.secondary", py: 4, textAlign: "center" }}>
              Loading schema details...
            </Typography>
          )}
          {viewTarget && !viewLoading && (
            <Box sx={{ display: "flex", flexDirection: "column", gap: 2, pt: 1 }}>
              {/* Metadata row */}
              <Box sx={{ display: "flex", gap: 2, flexWrap: "wrap" }}>
                <Box>
                  <Typography variant="caption" sx={{ color: "text.secondary", display: "block" }}>
                    Type
                  </Typography>
                  <Chip
                    label={viewTarget.type}
                    size="small"
                    variant="outlined"
                    color={viewTarget.type === "structured" ? "primary" : "secondary"}
                  />
                </Box>
                <Box>
                  <Typography variant="caption" sx={{ color: "text.secondary", display: "block" }}>
                    Status
                  </Typography>
                  <Chip
                    label={viewTarget.is_active ? "Active" : "Inactive"}
                    size="small"
                    color={viewTarget.is_active ? "success" : "default"}
                  />
                </Box>
                <Box>
                  <Typography variant="caption" sx={{ color: "text.secondary", display: "block" }}>
                    Created
                  </Typography>
                  <Typography variant="body2">{formatDate(viewTarget.created_at)}</Typography>
                </Box>
                <Box>
                  <Typography variant="caption" sx={{ color: "text.secondary", display: "block" }}>
                    Updated
                  </Typography>
                  <Typography variant="body2">{formatDate(viewTarget.updated_at)}</Typography>
                </Box>
              </Box>

              {/* JSON Schema */}
              <Box>
                <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
                  JSON Schema
                </Typography>
                <Box
                  sx={{
                    backgroundColor: "grey.900",
                    borderRadius: 1,
                    p: 2,
                    maxHeight: 300,
                    overflow: "auto",
                  }}
                >
                  <Typography
                    variant="body2"
                    component="pre"
                    sx={{
                      fontFamily: "monospace",
                      fontSize: "0.8125rem",
                      color: "common.white",
                      m: 0,
                      whiteSpace: "pre-wrap",
                      wordBreak: "break-word",
                    }}
                  >
                    {JSON.stringify(viewTarget.json_schema, null, 2)}
                  </Typography>
                </Box>
              </Box>

              {/* Prompt Template */}
              {viewTarget.prompt_template && (
                <Box>
                  <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
                    Prompt Template
                  </Typography>
                  <Box
                    sx={{
                      backgroundColor: "grey.900",
                      borderRadius: 1,
                      p: 2,
                      maxHeight: 200,
                      overflow: "auto",
                    }}
                  >
                    <Typography
                      variant="body2"
                      component="pre"
                      sx={{
                        fontFamily: "monospace",
                        fontSize: "0.8125rem",
                        color: "common.white",
                        m: 0,
                        whiteSpace: "pre-wrap",
                        wordBreak: "break-word",
                      }}
                    >
                      {viewTarget.prompt_template}
                    </Typography>
                  </Box>
                </Box>
              )}
            </Box>
          )}
        </DialogContent>
        <DialogActions>
          <Button
            onClick={() => {
              setViewDialogOpen(false);
              setViewTarget(null);
            }}
          >
            Close
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
        <DialogTitle>Delete Schema</DialogTitle>
        <DialogContent>
          <Typography variant="body1">
            Are you sure you want to delete schema{" "}
            <Typography component="strong" sx={{ fontWeight: 600 }}>
              {deleteTarget?.name}
            </Typography>
            ? This will mark it as inactive. Existing extractions referencing
            this schema are preserved.
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
            onClick={handleDeleteSchema}
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
