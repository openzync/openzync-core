"use client";
import { RequireAuth } from "../require-auth";

import Link from "next/link";
import { useEffect, useState, useCallback } from "react";
import { Plus, Copy, Edit, Trash2, RefreshCw, UsersIcon, X, CheckCircle, AlertCircle, Eye } from "lucide-react";
import { cn } from "@/lib/utils";

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

interface ToastState {
  visible: boolean;
  message: string;
  type: "success" | "error";
}

// ╔══════════════════════════════════════════════════════════════════════════════╗
// ║ Helpers                                                                     ║
// ╚══════════════════════════════════════════════════════════════════════════════╝

function formatDate(dateStr: string): string {
  const d = new Date(dateStr);
  return d.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function timeAgo(dateStr: string): string {
  const seconds = Math.floor((Date.now() - new Date(dateStr).getTime()) / 1000);
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const mins = Math.floor(seconds / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

// ╔══════════════════════════════════════════════════════════════════════════════╗
// ║ Auth fetch helper                                                           ║
// ╚══════════════════════════════════════════════════════════════════════════════╝

const API_BASE = "http://localhost:8000";

function authHeaders(): Record<string, string> {
  const token = sessionStorage.getItem("mg_access_token");
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  return headers;
}

// ╔══════════════════════════════════════════════════════════════════════════════╗
// ║ Loading spinner                                                             ║
// ╚══════════════════════════════════════════════════════════════════════════════╝

function Spinner({ className }: { className?: string }) {
  return (
    <svg
      className={cn("animate-spin h-4 w-4", className)}
      xmlns="http://www.w3.org/2000/svg"
      fill="none"
      viewBox="0 0 24 24"
    >
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path
        className="opacity-75"
        fill="currentColor"
        d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
      />
    </svg>
  );
}

// ╔══════════════════════════════════════════════════════════════════════════════╗
// ║ Dialog — Create / Edit User                                                ║
// ╚══════════════════════════════════════════════════════════════════════════════╝

interface UserFormData {
  external_id: string;
  name: string;
  email: string;
}

interface UserFormDialogProps {
  mode: "create" | "edit";
  initial: UserFormData;
  editId?: string;
  onClose: () => void;
  onSubmit: (data: UserFormData) => Promise<void>;
}

function UserFormDialog({ mode, initial, editId, onClose, onSubmit }: UserFormDialogProps) {
  const [form, setForm] = useState<UserFormData>(initial);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleChange = (field: keyof UserFormData, value: string) => {
    setForm((prev) => ({ ...prev, [field]: value }));
    if (error) setError(null);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!form.external_id.trim()) {
      setError("External ID is required");
      return;
    }
    setSubmitting(true);
    try {
      await onSubmit(form);
    } catch {
      // Error handled by caller via toast
    } finally {
      setSubmitting(false);
    }
  };

  const title = mode === "create" ? "Create User" : "Edit User";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 animate-fade-in" onClick={onClose}>
      <div
        className="card-base w-full max-w-md p-6 shadow-xl shadow-black/40 animate-slide-up"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">{title}</h2>
          <button onClick={onClose} className="btn-ghost p-1 rounded-md text-surface-400 hover:text-white">
            <X size={18} />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          {/* External ID */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">
              External ID <span className="text-error">*</span>
            </label>
            <input
              className="input-base"
              placeholder="e.g. user-abc-123"
              value={form.external_id}
              onChange={(e) => handleChange("external_id", e.target.value)}
              disabled={mode === "edit"}
            />
          </div>

          {/* Name */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Name</label>
            <input
              className="input-base"
              placeholder="e.g. John Doe"
              value={form.name}
              onChange={(e) => handleChange("name", e.target.value)}
            />
          </div>

          {/* Email */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Email</label>
            <input
              className="input-base"
              type="email"
              placeholder="e.g. john@acme.com"
              value={form.email}
              onChange={(e) => handleChange("email", e.target.value)}
            />
          </div>

          {/* Validation error */}
          {error && (
            <div className="rounded-md bg-error/10 border border-error/30 px-3 py-2 text-sm text-error flex items-center gap-2">
              <AlertCircle size={14} />
              {error}
            </div>
          )}

          {/* Actions */}
          <div className="flex justify-end gap-3 pt-2">
            <button type="button" onClick={onClose} className="btn-secondary text-sm">
              Cancel
            </button>
            <button type="submit" disabled={submitting} className="btn-primary text-sm min-w-[100px] justify-center">
              {submitting ? (
                <span className="flex items-center gap-2">
                  <Spinner /> {mode === "create" ? "Creating..." : "Saving..."}
                </span>
              ) : mode === "create" ? (
                "Create User"
              ) : (
                "Save Changes"
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ╔══════════════════════════════════════════════════════════════════════════════╗
// ║ Dialog — Delete Confirmation                                               ║
// ╚══════════════════════════════════════════════════════════════════════════════╝

interface DeleteDialogProps {
  userName: string;
  onClose: () => void;
  onConfirm: () => Promise<void>;
}

function DeleteDialog({ userName, onClose, onConfirm }: DeleteDialogProps) {
  const [submitting, setSubmitting] = useState(false);

  const handleConfirm = async () => {
    setSubmitting(true);
    try {
      await onConfirm();
    } catch {
      // Error handled by caller
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 animate-fade-in" onClick={onClose}>
      <div
        className="card-base w-full max-w-sm p-6 shadow-xl shadow-black/40 animate-slide-up"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-3 mb-4">
          <div className="flex h-10 w-10 items-center justify-center rounded-full bg-error/10 shrink-0">
            <Trash2 size={18} className="text-error" />
          </div>
          <div>
            <h2 className="text-lg font-semibold">Delete User</h2>
            <p className="text-sm text-surface-400">This action cannot be undone.</p>
          </div>
        </div>

        <p className="text-sm text-surface-300 mb-2">
          Are you sure you want to delete this user?
        </p>
        {userName && (
          <p className="text-sm font-medium text-white mb-5">
            &ldquo;{userName}&rdquo;
          </p>
        )}

        <div className="flex justify-end gap-3">
          <button onClick={onClose} className="btn-secondary text-sm">
            Cancel
          </button>
          <button onClick={handleConfirm} disabled={submitting} className="btn-danger text-sm min-w-[100px] justify-center">
            {submitting ? (
              <span className="flex items-center gap-2">
                <Spinner /> Deleting...
              </span>
            ) : (
              "Delete"
            )}
          </button>
        </div>
      </div>
    </div>
  );
}

// ╔══════════════════════════════════════════════════════════════════════════════╗
// ║ Toast Notification                                                         ║
// ╚══════════════════════════════════════════════════════════════════════════════╝

const TOAST_DURATION = 3000;

function Toast({ toast, onDismiss }: { toast: ToastState; onDismiss: () => void }) {
  useEffect(() => {
    if (!toast.visible) return;
    const timer = setTimeout(onDismiss, TOAST_DURATION);
    return () => clearTimeout(timer);
  }, [toast.visible, onDismiss]);

  if (!toast.visible) return null;

  const isSuccess = toast.type === "success";

  return (
    <div className="fixed bottom-6 right-6 z-[60] animate-slide-up">
      <div
        className={cn(
          "flex items-center gap-3 rounded-lg px-4 py-3 shadow-lg shadow-black/30 border min-w-[280px] max-w-sm",
          isSuccess
            ? "bg-surface-900 border-success/40 text-success"
            : "bg-surface-900 border-error/40 text-error",
        )}
      >
        {isSuccess ? <CheckCircle size={18} /> : <AlertCircle size={18} />}
        <span className="text-sm text-white flex-1">{toast.message}</span>
        <button onClick={onDismiss} className="text-surface-400 hover:text-white shrink-0">
          <X size={14} />
        </button>
      </div>
    </div>
  );
}

// ╔══════════════════════════════════════════════════════════════════════════════╗
// ║ Main Page                                                                  ║
// ╚══════════════════════════════════════════════════════════════════════════════╝

export default function UsersPage() {
  const [users, setUsers] = useState<UserItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);

  // Dialog state
  const [showCreate, setShowCreate] = useState(false);
  const [editTarget, setEditTarget] = useState<UserItem | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<UserItem | null>(null);

  // Toast state
  const [toast, setToast] = useState<ToastState>({ visible: false, message: "", type: "success" });

  // ── Fetch helpers ──────────────────────────────────────────────────────────

  const showToast = useCallback((message: string, type: "success" | "error") => {
    setToast({ visible: true, message, type });
  }, []);

  const dismissToast = useCallback(() => {
    setToast((prev) => ({ ...prev, visible: false }));
  }, []);

  const fetchUsers = useCallback(async (cursor?: string) => {
    const params = new URLSearchParams({ limit: "50" });
    if (cursor) params.set("cursor", cursor);

    const res = await fetch(`${API_BASE}/v1/users?${params}`, { headers: authHeaders() });
    if (!res.ok) throw new Error("Failed to fetch users");
    return (await res.json()) as UsersResponse;
  }, []);

  const loadUsers = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchUsers();
      setUsers(data.data);
      setNextCursor(data.next_cursor);
      setHasMore(data.has_more);
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Failed to load users", "error");
    } finally {
      setLoading(false);
    }
  }, [fetchUsers, showToast]);

  const loadMore = useCallback(async () => {
    if (!nextCursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const data = await fetchUsers(nextCursor);
      setUsers((prev) => [...prev, ...data.data]);
      setNextCursor(data.next_cursor);
      setHasMore(data.has_more);
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Failed to load more users", "error");
    } finally {
      setLoadingMore(false);
    }
  }, [nextCursor, loadingMore, fetchUsers, showToast]);

  // Initial load
  useEffect(() => {
    loadUsers();
  }, [loadUsers]);

  // ── CRUD handlers ──────────────────────────────────────────────────────────

  const handleCreate = async (data: UserFormData) => {
    const payload: Record<string, string> = { external_id: data.external_id.trim() };
    if (data.name.trim()) payload.name = data.name.trim();
    if (data.email.trim()) payload.email = data.email.trim();

    const res = await fetch(`${API_BASE}/v1/users`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify(payload),
    });

    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail ?? "Failed to create user");
    }

    setShowCreate(false);
    showToast(`User "${data.external_id}" created successfully`, "success");
    await loadUsers();
  };

  const handleUpdate = async (data: UserFormData) => {
    if (!editTarget) return;

    const payload: Record<string, string> = {};
    if (data.name.trim()) payload.name = data.name.trim();
    if (data.email.trim()) payload.email = data.email.trim();

    const res = await fetch(`${API_BASE}/v1/users/${editTarget.id}`, {
      method: "PATCH",
      headers: authHeaders(),
      body: JSON.stringify(payload),
    });

    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail ?? "Failed to update user");
    }

    setEditTarget(null);
    showToast(`User "${data.external_id}" updated successfully`, "success");
    await loadUsers();
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;

    const res = await fetch(`${API_BASE}/v1/users/${deleteTarget.id}`, {
      method: "DELETE",
      headers: authHeaders(),
    });

    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail ?? "Failed to delete user");
    }

    setDeleteTarget(null);
    showToast(`User "${deleteTarget.external_id}" deleted`, "success");
    await loadUsers();
  };

  // ── Clipboard ──────────────────────────────────────────────────────────────

  const copyToClipboard = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      showToast("User ID copied to clipboard", "success");
    } catch {
      showToast("Failed to copy", "error");
    }
  };

  // ── Skeleton rows ──────────────────────────────────────────────────────────

  const skeletonRows = Array.from({ length: 5 }, (_, i) => (
    <tr key={`skel-${i}`} className={i % 2 === 0 ? "bg-surface-950/50" : ""}>
      {[1, 2, 3, 4, 5].map((col) => (
        <td key={col} className="px-4 py-3">
          <div className="h-4 rounded bg-surface-800 animate-pulse" style={{ width: col === 3 ? "120px" : "80px" }} />
        </td>
      ))}
    </tr>
  ));

  // ── Empty state ────────────────────────────────────────────────────────────

  const emptyRow = (
    <tr>
      <td colSpan={5}>
        <div className="flex flex-col items-center justify-center py-16 text-surface-500">
          <UsersIcon size={40} className="mb-3 opacity-40" />
          <p className="text-sm font-medium">No users found</p>
          <p className="text-xs mt-1">Create your first user to get started</p>
        </div>
      </td>
    </tr>
  );

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
    <RequireAuth>
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Users</h1>
          <p className="text-sm text-surface-400 mt-1">Manage end-users within your organization</p>
        </div>
        <button onClick={() => setShowCreate(true)} className="btn-primary text-sm">
          <Plus size={16} />
          Create User
        </button>
      </div>

      {/* Users table */}
      <div className="card-base overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            {/* Header */}
            <thead>
              <tr className="bg-surface-800">
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">
                  External ID
                </th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">
                  Name
                </th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">
                  Email
                </th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">
                  Created
                </th>
                <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-surface-400">
                  Actions
                </th>
              </tr>
            </thead>

            {/* Body */}
            <tbody className="divide-y divide-surface-800">
              {loading
                ? skeletonRows
                : users.length === 0
                  ? emptyRow
                  : users.map((user, idx) => (
                      <tr
                        key={user.id}
                        className={cn(
                          "transition-colors hover:bg-surface-800/50",
                          idx % 2 === 0 ? "bg-surface-950/50" : "",
                        )}
                      >
                        {/* External ID */}
                        <td className="px-4 py-3">
                          <span className="font-mono text-xs text-white">{user.external_id}</span>
                        </td>

                        {/* Name */}
                        <td className="px-4 py-3 text-surface-200">
                          {user.name || <span className="text-surface-500 italic">—</span>}
                        </td>

                        {/* Email */}
                        <td className="px-4 py-3 text-surface-200">
                          {user.email || <span className="text-surface-500 italic">—</span>}
                        </td>

                        {/* Created */}
                        <td className="px-4 py-3">
                          <div className="flex flex-col">
                            <span className="text-surface-200 text-xs">{formatDate(user.created_at)}</span>
                            <span className="text-surface-500 text-[11px]">{timeAgo(user.created_at)}</span>
                          </div>
                        </td>

                        {/* Actions */}
                        <td className="px-4 py-3 text-right">
                          <div className="flex items-center justify-end gap-1">
                            <Link
                              href={`/users/${user.id}`}
                              className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-white"
                              title="View User"
                            >
                              <Eye size={14} />
                            </Link>
                            <button
                              onClick={() => copyToClipboard(user.id)}
                              className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-white"
                              title="Copy User ID"
                            >
                              <Copy size={14} />
                            </button>
                            <button
                              onClick={() => setEditTarget(user)}
                              className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-white"
                              title="Edit User"
                            >
                              <Edit size={14} />
                            </button>
                            <button
                              onClick={() => setDeleteTarget(user)}
                              className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-error"
                              title="Delete User"
                            >
                              <Trash2 size={14} />
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))}
            </tbody>
          </table>
        </div>

        {/* Load more */}
        {!loading && hasMore && (
          <div className="border-t border-surface-800 px-4 py-3 text-center">
            <button
              onClick={loadMore}
              disabled={loadingMore}
              className="btn-ghost text-xs text-surface-400 hover:text-white gap-2"
            >
              {loadingMore ? (
                <span className="flex items-center gap-2">
                  <Spinner /> Loading...
                </span>
              ) : (
                <span className="flex items-center gap-2">
                  <RefreshCw size={14} /> Load More
                </span>
              )}
            </button>
          </div>
        )}
      </div>

      {/* ── Create Dialog ──────────────────────────────────────────────────── */}
      {showCreate && (
        <UserFormDialog
          mode="create"
          initial={{ external_id: "", name: "", email: "" }}
          onClose={() => setShowCreate(false)}
          onSubmit={handleCreate}
        />
      )}

      {/* ── Edit Dialog ─────────────────────────────────────────────────────── */}
      {editTarget && (
        <UserFormDialog
          mode="edit"
          editId={editTarget.id}
          initial={{
            external_id: editTarget.external_id,
            name: editTarget.name ?? "",
            email: editTarget.email ?? "",
          }}
          onClose={() => setEditTarget(null)}
          onSubmit={handleUpdate}
        />
      )}

      {/* ── Delete Dialog ───────────────────────────────────────────────────── */}
      {deleteTarget && (
        <DeleteDialog
          userName={deleteTarget.name || deleteTarget.external_id}
          onClose={() => setDeleteTarget(null)}
          onConfirm={handleDelete}
        />
      )}

      {/* ── Toast ───────────────────────────────────────────────────────────── */}
      <Toast toast={toast} onDismiss={dismissToast} />
    </div>
  </RequireAuth>
  );
}
