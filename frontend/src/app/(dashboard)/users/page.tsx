"use client";
import { RequireAuth } from "../require-auth";

import Link from "next/link";
import { useEffect, useState, useCallback } from "react";
import {
  Plus, Copy, Edit, Trash2, RefreshCw, UsersIcon, X, AlertCircle, Eye,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { get, post, patch as apiPatch, del as apiDel, ApiError } from "@/lib/api-client";
import { formatDate, timeAgo, copyToClipboard } from "@/lib/utils";
import { toast } from "sonner";
import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/empty-state";
import { ErrorState } from "@/components/shared/error-state";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { TableSkeleton } from "@/components/shared/skeleton";

// ─── Types ─────────────────────────────────────────────────────────────────────

interface UserItem {
  id: string;
  external_id: string;
  name: string | null;
  email: string | null;
  is_active: boolean;
  created_at: string;
}

interface UsersResponse {
  data: UserItem[];
  next_cursor: string | null;
  has_more: boolean;
}

interface UserFormData {
  external_id: string;
  name: string;
  email: string;
}

// ─── User Form Dialog (Create / Edit) ──────────────────────────────────────────

function UserFormDialog({
  mode, initial, editId, onClose, onSubmit,
}: {
  mode: "create" | "edit";
  initial: UserFormData;
  editId?: string;
  onClose: () => void;
  onSubmit: (data: UserFormData) => Promise<void>;
}) {
  const [form, setForm] = useState<UserFormData>(initial);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleChange = (field: keyof UserFormData, value: string) => {
    setForm((prev) => ({ ...prev, [field]: value }));
    if (error) setError(null);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!form.external_id.trim()) { setError("External ID is required"); return; }
    setSubmitting(true);
    try { await onSubmit(form); }
    catch { /* handled by caller */ }
    finally { setSubmitting(false); }
  };

  const title = mode === "create" ? "Create User" : "Edit User";

  return (
    <>
      <div className="fixed inset-0 z-50 bg-black/50 animate-fade-in" onClick={onClose} />
      <div className="fixed left-1/2 top-1/2 z-50 w-full max-w-md -translate-x-1/2 -translate-y-1/2 rounded-lg border border-surface-800 bg-surface-900 p-6 shadow-xl shadow-black/40 animate-slide-up">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-text-primary">{title}</h2>
          <button onClick={onClose} className="text-surface-400 hover:text-white p-1"><X size={18} /></button>
        </div>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">External ID <span className="text-error">*</span></label>
            <input className="input-base" placeholder="e.g. user-abc-123" value={form.external_id}
              onChange={(e) => handleChange("external_id", e.target.value)} disabled={mode === "edit"} />
          </div>
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Name</label>
            <input className="input-base" placeholder="e.g. John Doe" value={form.name}
              onChange={(e) => handleChange("name", e.target.value)} />
          </div>
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Email</label>
            <input className="input-base" type="email" placeholder="e.g. john@acme.com" value={form.email}
              onChange={(e) => handleChange("email", e.target.value)} />
          </div>
          {error && (
            <div className="rounded-md bg-error/10 border border-error/30 px-3 py-2 text-sm text-error flex items-center gap-2">
              <AlertCircle size={14} />{error}
            </div>
          )}
          <div className="flex justify-end gap-3 pt-2">
            <Button variant="secondary" size="sm" onClick={onClose}>Cancel</Button>
            <Button variant="primary" size="sm" type="submit" loading={submitting}>
              {mode === "create" ? "Create User" : "Save Changes"}
            </Button>
          </div>
        </form>
      </div>
    </>
  );
}

// ─── Main Page ─────────────────────────────────────────────────────────────────

export default function UsersPage() {
  const [users, setUsers] = useState<UserItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);

  // Dialog state
  const [showCreate, setShowCreate] = useState(false);
  const [editTarget, setEditTarget] = useState<UserItem | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<UserItem | null>(null);
  const [deleting, setDeleting] = useState(false);

  // ── Fetch ──────────────────────────────────────────────────────────────────

  const fetchUsers = useCallback(async (cursor?: string): Promise<UsersResponse> => {
    const params = new URLSearchParams({ limit: "50" });
    if (cursor) params.set("cursor", cursor);
    return get<UsersResponse>(`/v1/users?${params}`);
  }, []);

  const loadUsers = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const data = await fetchUsers();
      setUsers(data.data);
      setNextCursor(data.next_cursor);
      setHasMore(data.has_more);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load users");
    } finally { setLoading(false); }
  }, [fetchUsers]);

  const loadMore = useCallback(async () => {
    if (!nextCursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const data = await fetchUsers(nextCursor);
      setUsers((prev) => [...prev, ...data.data]);
      setNextCursor(data.next_cursor);
      setHasMore(data.has_more);
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Failed to load more users");
    } finally { setLoadingMore(false); }
  }, [nextCursor, loadingMore, fetchUsers]);

  useEffect(() => { loadUsers(); }, [loadUsers]);

  // ── CRUD ───────────────────────────────────────────────────────────────────

  const handleCreate = async (data: UserFormData) => {
    const payload: Record<string, string> = { external_id: data.external_id.trim() };
    if (data.name.trim()) payload.name = data.name.trim();
    if (data.email.trim()) payload.email = data.email.trim();
    await post("/v1/users", payload);
    setShowCreate(false);
    toast.success(`User "${data.external_id}" created successfully`);
    await loadUsers();
  };

  const handleUpdate = async (data: UserFormData) => {
    if (!editTarget) return;
    const payload: Record<string, string> = {};
    if (data.name.trim()) payload.name = data.name.trim();
    if (data.email.trim()) payload.email = data.email.trim();
    await apiPatch(`/v1/users/${editTarget.id}`, payload);
    setEditTarget(null);
    toast.success(`User "${data.external_id}" updated successfully`);
    await loadUsers();
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await apiDel(`/v1/users/${deleteTarget.id}`);
      setDeleteTarget(null);
      toast.success(`User "${deleteTarget.external_id}" deleted`);
      await loadUsers();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Failed to delete user");
    } finally { setDeleting(false); }
  };

  const handleCopyId = async (id: string) => {
    const ok = await copyToClipboard(id);
    toast.success(ok ? "User ID copied to clipboard" : "Failed to copy");
  };

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
    <RequireAuth>
    <div className="space-y-6">
      <PageHeader
        title="Users"
        description="Manage end-users within your organization"
        actions={
          <Button variant="primary" size="sm" icon={<Plus size={14} />} onClick={() => setShowCreate(true)}>
            Create User
          </Button>
        }
      />

      {error && <ErrorState message={error} onRetry={loadUsers} />}

      {/* Users table */}
      <div className="card-base overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-surface-800">
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">External ID</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Name</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Email</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Created</th>
                <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-surface-400">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-surface-800">
              {loading ? (
                <TableSkeleton rows={5} cols={5} colWidths={["w-32", "w-24", "w-36", "w-28", "w-32"]} />
              ) : users.length === 0 ? (
                <tr><td colSpan={5}><EmptyState icon={UsersIcon} title="No users found" description="Create your first user to get started" /></td></tr>
              ) : (
                users.map((user, idx) => (
                  <tr key={user.id} className={cn("transition-colors hover:bg-surface-800/50", idx % 2 === 0 ? "bg-surface-950/50" : "")}>
                    <td className="px-4 py-3"><span className="font-mono text-xs text-white">{user.external_id}</span></td>
                    <td className="px-4 py-3 text-surface-200">
                      {user.name || <span className="text-surface-500 italic">—</span>}
                    </td>
                    <td className="px-4 py-3 text-surface-200">
                      {user.email || <span className="text-surface-500 italic">—</span>}
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex flex-col">
                        <span className="text-surface-200 text-xs">{formatDate(user.created_at)}</span>
                        <span className="text-surface-500 text-[11px]">{timeAgo(user.created_at)}</span>
                      </div>
                    </td>
                    <td className="px-4 py-3 text-right">
                      <div className="flex items-center justify-end gap-1">
                        <Link href={`/users/${user.id}`}><Button variant="ghost" size="sm" className="rounded-md text-surface-400 hover:text-white" title="View User"><Eye size={14} /></Button></Link>
                        <Button variant="ghost" size="sm" onClick={() => handleCopyId(user.id)} className="rounded-md text-surface-400 hover:text-white" title="Copy User ID"><Copy size={14} /></Button>
                        <Button variant="ghost" size="sm" onClick={() => setEditTarget(user)} className="rounded-md text-surface-400 hover:text-white" title="Edit User"><Edit size={14} /></Button>
                        <Button variant="ghost" size="sm" onClick={() => setDeleteTarget(user)} className="rounded-md text-surface-400 hover:text-error" title="Delete User"><Trash2 size={14} /></Button>
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        {/* Load more */}
        {!loading && hasMore && (
          <div className="border-t border-surface-800 px-4 py-3 text-center">
            <Button variant="ghost" size="sm" onClick={loadMore} disabled={loadingMore} className="text-surface-400 hover:text-white gap-2">
              {loadingMore ? <span className="flex items-center gap-2"><Spinner /> Loading...</span> : <span className="flex items-center gap-2"><RefreshCw size={14} /> Load More</span>}
            </Button>
          </div>
        )}
      </div>

      {/* Dialogs */}
      {showCreate && (
        <UserFormDialog mode="create" initial={{ external_id: "", name: "", email: "" }}
          onClose={() => setShowCreate(false)} onSubmit={handleCreate} />
      )}
      {editTarget && (
        <UserFormDialog mode="edit" editId={editTarget.id}
          initial={{ external_id: editTarget.external_id, name: editTarget.name ?? "", email: editTarget.email ?? "" }}
          onClose={() => setEditTarget(null)} onSubmit={handleUpdate} />
      )}
      <ConfirmDialog
        open={!!deleteTarget}
        title="Delete User"
        message={`Are you sure you want to delete user "${deleteTarget?.name || deleteTarget?.external_id}"? This action cannot be undone.`}
        confirmLabel="Delete"
        variant="danger"
        loading={deleting}
        onConfirm={handleDelete}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  </RequireAuth>
  );
}
