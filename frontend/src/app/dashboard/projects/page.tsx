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
  Chip,
  List,
  ListItem,
  ListItemText,
  Select,
  MenuItem,
  FormControl,
  InputLabel,
} from "@mui/material";
import { DataGrid, type GridColDef } from "@mui/x-data-grid";
import {
  Add as AddIcon,
  Delete as DeleteIcon,
  Edit as EditIcon,
  People as PeopleIcon,
} from "@mui/icons-material";
import {
  listProjects,
  getProject,
  createProject,
  updateProject,
  deleteProject,
  listProjectMembers,
  addProjectMember,
  updateProjectMemberRole,
  removeProjectMember,
  type ProjectResponse,
  type MemberResponse,
  ApiError,
} from "@/lib/api/client";
import { useAuth } from "@/lib/auth/useAuth";

// ─── Types ───────────────────────────────────────────────────────────────────

interface ProjectRow {
  id: string;
  name: string;
  description: string | null;
  is_active: boolean;
  created_at: string;
}

interface CreateEditForm {
  name: string;
  description: string;
}

// ─── Page ────────────────────────────────────────────────────────────────────

export default function ProjectsPage() {
  const { user } = useAuth();

  // Data state
  const [projects, setProjects] = useState<ProjectRow[]>([]);
  const [loading, setLoading] = useState(true);

  // Create/Edit dialog
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<CreateEditForm>({ name: "", description: "" });
  const [saving, setSaving] = useState(false);

  // Members dialog
  const [membersDialogOpen, setMembersDialogOpen] = useState(false);
  const [selectedProject, setSelectedProject] = useState<ProjectResponse | null>(null);
  const [members, setMembers] = useState<MemberResponse[]>([]);
  const [membersLoading, setMembersLoading] = useState(false);

  // Add member dialog
  const [addMemberOpen, setAddMemberOpen] = useState(false);
  const [newMemberId, setNewMemberId] = useState("");
  const [newMemberRole, setNewMemberRole] = useState("member");

  // Snackbar
  const [snackbar, setSnackbar] = useState<{ message: string; severity: "success" | "error" } | null>(null);

  // ── Data Fetching ──────────────────────────────────────────────────────────

  const fetchProjects = useCallback(async () => {
    try {
      setLoading(true);
      const res = await listProjects();
      setProjects(res.data || []);
    } catch (err) {
      setSnackbar({ message: "Failed to load projects", severity: "error" });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchProjects();
  }, [fetchProjects]);

  // ── Create / Edit ──────────────────────────────────────────────────────────

  const openCreateDialog = () => {
    setEditingId(null);
    setForm({ name: "", description: "" });
    setDialogOpen(true);
  };

  const openEditDialog = (project: ProjectRow) => {
    setEditingId(project.id);
    setForm({ name: project.name, description: project.description || "" });
    setDialogOpen(true);
  };

  const handleSave = async () => {
    if (!form.name.trim()) {
      setSnackbar({ message: "Project name is required", severity: "error" });
      return;
    }
    try {
      setSaving(true);
      if (editingId) {
        await updateProject(editingId, { name: form.name, description: form.description || undefined });
        setSnackbar({ message: "Project updated", severity: "success" });
      } else {
        await createProject(form.name, form.description || undefined);
        setSnackbar({ message: "Project created", severity: "success" });
      }
      setDialogOpen(false);
      await fetchProjects();
    } catch (err) {
      const msg = err instanceof ApiError ? err.detail || "Operation failed" : "Operation failed";
      setSnackbar({ message: msg, severity: "error" });
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (id: string) => {
    if (!confirm("Delete this project? This cannot be undone.")) return;
    try {
      await deleteProject(id);
      setSnackbar({ message: "Project deleted", severity: "success" });
      await fetchProjects();
    } catch (err) {
      setSnackbar({ message: "Failed to delete project", severity: "error" });
    }
  };

  // ── Members ────────────────────────────────────────────────────────────────

  const openMembersDialog = async (project: ProjectRow) => {
    try {
      setSelectedProject(project as ProjectResponse);
      setMembersDialogOpen(true);
      setMembersLoading(true);
      const fullProject = await getProject(project.id);
      setSelectedProject(fullProject);
      const res = await listProjectMembers(project.id);
      setMembers(res.members || []);
    } catch (err) {
      setSnackbar({ message: "Failed to load members", severity: "error" });
    } finally {
      setMembersLoading(false);
    }
  };

  const handleAddMember = async () => {
    if (!newMemberId.trim() || !selectedProject) return;
    try {
      await addProjectMember(selectedProject.id, newMemberId, newMemberRole);
      setSnackbar({ message: "Member added", severity: "success" });
      setAddMemberOpen(false);
      setNewMemberId("");
      const res = await listProjectMembers(selectedProject.id);
      setMembers(res.members || []);
    } catch (err) {
      const msg = err instanceof ApiError ? err.detail || "Failed to add member" : "Failed to add member";
      setSnackbar({ message: msg, severity: "error" });
    }
  };

  const handleRemoveMember = async (userId: string) => {
    if (!selectedProject || !confirm("Remove this member from the project?")) return;
    try {
      await removeProjectMember(selectedProject.id, userId);
      setSnackbar({ message: "Member removed", severity: "success" });
      const res = await listProjectMembers(selectedProject.id);
      setMembers(res.members || []);
    } catch (err) {
      setSnackbar({ message: "Failed to remove member", severity: "error" });
    }
  };

  const handleUpdateRole = async (userId: string, role: string) => {
    if (!selectedProject) return;
    try {
      await updateProjectMemberRole(selectedProject.id, userId, role);
      setSnackbar({ message: "Role updated", severity: "success" });
      const res = await listProjectMembers(selectedProject.id);
      setMembers(res.members || []);
    } catch (err) {
      setSnackbar({ message: "Failed to update role", severity: "error" });
    }
  };

  // ── Columns ────────────────────────────────────────────────────────────────

  const columns: GridColDef[] = [
    { field: "name", headerName: "Name", flex: 1, minWidth: 150 },
    {
      field: "description",
      headerName: "Description",
      flex: 2,
      minWidth: 200,
      renderCell: ({ value }) => value || "—",
    },
    {
      field: "is_active",
      headerName: "Status",
      width: 100,
      renderCell: ({ value }) => (
        <Chip label={value ? "Active" : "Inactive"} color={value ? "success" : "default"} size="small" />
      ),
    },
    {
      field: "created_at",
      headerName: "Created",
      width: 180,
      renderCell: ({ value }) => new Date(value).toLocaleDateString(),
    },
    {
      field: "actions",
      headerName: "Actions",
      width: 180,
      sortable: false,
      renderCell: ({ row }) => (
        <Box sx={{ display: "flex", gap: 0.5 }}>
          <Tooltip title="Manage members">
            <IconButton size="small" onClick={() => openMembersDialog(row)}>
              <PeopleIcon fontSize="small" />
            </IconButton>
          </Tooltip>
          <Tooltip title="Edit project">
            <IconButton size="small" onClick={() => openEditDialog(row)}>
              <EditIcon fontSize="small" />
            </IconButton>
          </Tooltip>
          <Tooltip title="Delete project">
            <IconButton size="small" onClick={() => handleDelete(row.id)}>
              <DeleteIcon fontSize="small" />
            </IconButton>
          </Tooltip>
        </Box>
      ),
    },
  ];

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
    <Box sx={{ p: 3 }}>
      <Box sx={{ display: "flex", justifyContent: "space-between", alignItems: "center", mb: 3 }}>
        <Typography variant="h4">Projects</Typography>
        <Button variant="contained" startIcon={<AddIcon />} onClick={openCreateDialog}>
          New Project
        </Button>
      </Box>

      <Card>
        <DataGrid
          rows={projects}
          columns={columns}
          loading={loading}
          autoHeight
          disableRowSelectionOnClick
          pageSizeOptions={[10, 25, 50]}
          initialState={{ pagination: { paginationModel: { pageSize: 10 } } }}
          getRowId={(row) => row.id}
        />
      </Card>

      {/* ── Create / Edit Dialog ────────────────────────────────────────────── */}
      <Dialog open={dialogOpen} onClose={() => setDialogOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>{editingId ? "Edit Project" : "Create Project"}</DialogTitle>
        <DialogContent>
          <TextField
            autoFocus
            label="Project Name"
            fullWidth
            required
            margin="normal"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
          />
          <TextField
            label="Description"
            fullWidth
            multiline
            rows={3}
            margin="normal"
            value={form.description}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDialogOpen(false)}>Cancel</Button>
          <Button onClick={handleSave} variant="contained" disabled={saving}>
            {saving ? <CircularProgress size={20} /> : editingId ? "Save" : "Create"}
          </Button>
        </DialogActions>
      </Dialog>

      {/* ── Members Dialog ──────────────────────────────────────────────────── */}
      <Dialog open={membersDialogOpen} onClose={() => setMembersDialogOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>
          Members{selectedProject ? ` — ${selectedProject.name}` : ""}
        </DialogTitle>
        <DialogContent>
          <Box sx={{ mb: 2, display: "flex", justifyContent: "flex-end" }}>
            <Button
              size="small"
              startIcon={<AddIcon />}
              onClick={() => setAddMemberOpen(true)}
            >
              Add Member
            </Button>
          </Box>

          {membersLoading ? (
            <Box sx={{ display: "flex", justifyContent: "center", py: 4 }}>
              <CircularProgress />
            </Box>
          ) : members.length === 0 ? (
            <Typography color="text.secondary" sx={{ py: 2, textAlign: "center" }}>
              No members yet.
            </Typography>
          ) : (
            <List>
              {members.map((m) => (
                <ListItem
                  key={m.user_id}
                  secondaryAction={
                    <Box sx={{ display: "flex", gap: 1, alignItems: "center" }}>
                      <FormControl size="small" sx={{ minWidth: 100 }}>
                        <Select
                          value={m.role}
                          onChange={(e) => handleUpdateRole(m.user_id, e.target.value)}
                        >
                          <MenuItem value="admin">Admin</MenuItem>
                          <MenuItem value="member">Member</MenuItem>
                          <MenuItem value="viewer">Viewer</MenuItem>
                        </Select>
                      </FormControl>
                      <IconButton
                        edge="end"
                        size="small"
                        onClick={() => handleRemoveMember(m.user_id)}
                      >
                        <DeleteIcon fontSize="small" />
                      </IconButton>
                    </Box>
                  }
                >
                  <ListItemText
                    primary={m.user_id}
                    secondary={`Role: ${m.role}`}
                    primaryTypographyProps={{ variant: "body2", sx: { fontFamily: "monospace", fontSize: "0.8rem" } }}
                  />
                </ListItem>
              ))}
            </List>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setMembersDialogOpen(false)}>Close</Button>
        </DialogActions>
      </Dialog>

      {/* ── Add Member Dialog ───────────────────────────────────────────────── */}
      <Dialog open={addMemberOpen} onClose={() => setAddMemberOpen(false)} maxWidth="xs" fullWidth>
        <DialogTitle>Add Member</DialogTitle>
        <DialogContent>
          <TextField
            autoFocus
            label="User ID"
            fullWidth
            margin="normal"
            value={newMemberId}
            onChange={(e) => setNewMemberId(e.target.value)}
          />
          <FormControl fullWidth margin="normal">
            <InputLabel>Role</InputLabel>
            <Select
              value={newMemberRole}
              label="Role"
              onChange={(e) => setNewMemberRole(e.target.value)}
            >
              <MenuItem value="member">Member</MenuItem>
              <MenuItem value="admin">Admin</MenuItem>
              <MenuItem value="viewer">Viewer</MenuItem>
            </Select>
          </FormControl>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setAddMemberOpen(false)}>Cancel</Button>
          <Button onClick={handleAddMember} variant="contained">
            Add
          </Button>
        </DialogActions>
      </Dialog>

      {/* ── Snackbar ────────────────────────────────────────────────────────── */}
      <Snackbar
        open={!!snackbar}
        autoHideDuration={4000}
        onClose={() => setSnackbar(null)}
        anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
      >
        {snackbar ? (
          <Alert severity={snackbar.severity} onClose={() => setSnackbar(null)}>
            {snackbar.message}
          </Alert>
        ) : undefined}
      </Snackbar>
    </Box>
  );
}
