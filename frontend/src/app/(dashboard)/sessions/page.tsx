"use client";
import { RequireAuth } from "../require-auth";

import { useState, useEffect, useCallback } from "react";
import { useRouter } from "next/navigation";
import {
  Plus,
  Eye,
  Trash2,
  Loader2,
  ChevronDown,
  X,
  AlertTriangle,
  MessageSquare,
} from "lucide-react";

// ─── Types ─────────────────────────────────────────────────────────────────────

interface User {
  id: string;
  external_id?: string;
  name?: string;
}

interface Session {
  id: string;
  user_id: string;
  external_id: string;
  is_active: boolean;
  message_count: number;
  fact_count: number;
  created_at: string;
  closed_at?: string;
}

interface SessionsApiResponse {
  data: Session[];
  next_cursor: string | null;
  has_more: boolean;
  total: number | null;
}

// ─── Helpers ───────────────────────────────────────────────────────────────────

function formatDate(dateStr: string): string {
  const date = new Date(dateStr);
  return date.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function getUserLabel(user: User): string {
  return user.external_id ?? user.name ?? user.id.slice(0, 8);
}

// ─── Page ──────────────────────────────────────────────────────────────────────

export default function SessionsPage() {
  const router = useRouter();

  // ── Users ─────────────────────────────────────────────────────────────────
  const [users, setUsers] = useState<User[]>([]);
  const [selectedUserId, setSelectedUserId] = useState<string>("");
  const [usersLoading, setUsersLoading] = useState(true);

  // ── Sessions ──────────────────────────────────────────────────────────────
  const [sessions, setSessions] = useState<Session[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [fetchError, setFetchError] = useState("");

  // ── Create dialog ─────────────────────────────────────────────────────────
  const [showCreateDialog, setShowCreateDialog] = useState(false);
  const [newExternalId, setNewExternalId] = useState("");
  const [newUserId, setNewUserId] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState("");

  // ── Delete dialog ─────────────────────────────────────────────────────────
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Session | null>(null);
  const [deleting, setDeleting] = useState(false);

  // ── Fetch users on mount ──────────────────────────────────────────────────
  useEffect(() => {
    async function fetchUsers() {
      try {
        const token = sessionStorage.getItem("mg_access_token");
        const headers: Record<string, string> = {};
        if (token) headers["Authorization"] = `Bearer ${token}`;

        const res = await fetch("http://localhost:8000/v1/users?limit=200", {
          headers,
        });
        if (res.ok) {
          const json = await res.json();
          const userList: User[] = json.data ?? json.items ?? [];
          setUsers(userList);
          if (userList.length > 0) {
            const firstId = userList[0].id;
            setSelectedUserId(firstId);
            setNewUserId(firstId);
          }
        }
      } catch {
        // Silently fail — user dropdown will be empty
      } finally {
        setUsersLoading(false);
      }
    }
    fetchUsers();
  }, []);

  // ⚠️ Potential race: if selectedUserId changes rapidly, old fetches may
  // overwrite newer ones. Use an AbortController pattern if this becomes an issue.
  // ── Fetch sessions when selectedUserId changes ────────────────────────────
  const fetchSessions = useCallback(
    async (userId: string, cursorVal: string | null) => {
      if (!userId) return;

      const isInitial = !cursorVal;
      if (isInitial) {
        setLoading(true);
        setFetchError("");
      } else {
        setLoadingMore(true);
      }

      try {
        const token = sessionStorage.getItem("mg_access_token");
        const headers: Record<string, string> = {};
        if (token) headers["Authorization"] = `Bearer ${token}`;

        let url = `http://localhost:8000/v1/users/${userId}/sessions?limit=50&include_closed=true`;
        if (cursorVal) url += `&cursor=${encodeURIComponent(cursorVal)}`;

        const res = await fetch(url, { headers });
        if (res.ok) {
          const json: SessionsApiResponse = await res.json();
          const items = json.data ?? [];
          if (isInitial) {
            setSessions(items);
          } else {
            setSessions((prev) => [...prev, ...items]);
          }
          setCursor(json.next_cursor ?? null);
          setHasMore(json.has_more ?? false);
        } else if (isInitial) {
          const err = await res.json().catch(() => ({}));
          setFetchError(err.detail ?? "Failed to load sessions");
          setSessions([]);
        }
      } catch {
        if (isInitial) {
          setFetchError("Network error loading sessions");
          setSessions([]);
        }
      } finally {
        setLoading(false);
        setLoadingMore(false);
      }
    },
    [],
  );

  useEffect(() => {
    if (selectedUserId) {
      fetchSessions(selectedUserId, null);
    }
  }, [selectedUserId, fetchSessions]);

  // ── Create session ────────────────────────────────────────────────────────
  async function handleCreateSession() {
    const trimmedId = newExternalId.trim();
    if (!trimmedId) {
      setCreateError("External ID is required");
      return;
    }

    setCreating(true);
    setCreateError("");

    try {
      const token = sessionStorage.getItem("mg_access_token");
      const headers: Record<string, string> = {
        "Content-Type": "application/json",
      };
      if (token) headers["Authorization"] = `Bearer ${token}`;

      const targetUserId = newUserId || selectedUserId;

      const res = await fetch(
        `http://localhost:8000/v1/users/${targetUserId}/sessions`,
        {
          method: "POST",
          headers,
          body: JSON.stringify({ external_id: trimmedId }),
        },
      );

      if (res.ok) {
        setShowCreateDialog(false);
        setNewExternalId("");
        // Reset to first page to include the new session
        fetchSessions(selectedUserId, null);
      } else {
        const err = await res.json().catch(() => ({}));
        setCreateError(err.detail ?? `Failed to create session (${res.status})`);
      }
    } catch {
      setCreateError("Network error. Please try again.");
    } finally {
      setCreating(false);
    }
  }

  // ── Delete session ────────────────────────────────────────────────────────
  async function handleDeleteSession() {
    if (!deleteTarget) return;

    setDeleting(true);

    try {
      const token = sessionStorage.getItem("mg_access_token");
      const headers: Record<string, string> = {
        "Content-Type": "application/json",
      };
      if (token) headers["Authorization"] = `Bearer ${token}`;

      const res = await fetch(
        `http://localhost:8000/v1/users/${deleteTarget.user_id}/sessions/${deleteTarget.id}`,
        { method: "DELETE", headers },
      );

      if (res.ok) {
        setSessions((prev) => prev.filter((s) => s.id !== deleteTarget.id));
        setShowDeleteDialog(false);
        setDeleteTarget(null);
      }
    } catch {
      // Silently fail — item still shows, user can retry
    } finally {
      setDeleting(false);
    }
  }

  // ── Handlers ──────────────────────────────────────────────────────────────

  function openDeleteDialog(session: Session) {
    setDeleteTarget(session);
    setShowDeleteDialog(true);
  }

  function openCreateDialog() {
    setNewExternalId("");
    setNewUserId(selectedUserId);
    setCreateError("");
    setShowCreateDialog(true);
  }

  function handleUserChange(e: React.ChangeEvent<HTMLSelectElement>) {
    setSelectedUserId(e.target.value);
  }

  // ── Render helpers ────────────────────────────────────────────────────────

  function renderStatusChip(session: Session) {
    const isActive = session.is_active;
    return (
      <span
        className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${
          isActive
            ? "bg-success/10 text-success"
            : "bg-surface-700 text-surface-400"
        }`}
      >
        <span
          className={`mr-1.5 h-1.5 w-1.5 rounded-full ${
            isActive ? "bg-success" : "bg-surface-500"
          }`}
        />
        {isActive ? "Active" : "Closed"}
      </span>
    );
  }

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <RequireAuth>
    <div className="space-y-6">
      {/* ═══ Page header ═══ */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Sessions</h1>
          <p className="text-sm text-surface-400 mt-1">
            Conversation sessions with extracted memory
          </p>
        </div>
        <button
          onClick={openCreateDialog}
          className="btn-primary text-sm"
        >
          <Plus size={16} />
          Create Session
        </button>
      </div>

      {/* ═══ Top bar — User dropdown ═══ */}
      <div className="flex items-center gap-3">
        <label className="text-sm text-surface-400 font-medium shrink-0">
          User
        </label>
        <div className="relative w-64">
          <select
            value={selectedUserId}
            onChange={handleUserChange}
            disabled={usersLoading || users.length === 0}
            className="input-base appearance-none pr-8 cursor-pointer disabled:cursor-not-allowed"
          >
            {usersLoading ? (
              <option value="">Loading users…</option>
            ) : users.length === 0 ? (
              <option value="">No users found</option>
            ) : (
              users.map((user) => (
                <option key={user.id} value={user.id}>
                  {getUserLabel(user)}
                </option>
              ))
            )}
          </select>
          <ChevronDown
            size={14}
            className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-surface-400"
          />
        </div>
        {users.length > 0 && (
          <span className="text-xs text-surface-500">
            {sessions.length} session{sessions.length !== 1 ? "s" : ""}
          </span>
        )}
      </div>

      {/* ═══ Sessions table ═══ */}
      <div className="card-base overflow-hidden">
        {loading ? (
          /* Loading skeleton */
          <div className="p-6 space-y-4">
            {[1, 2, 3, 4, 5].map((i) => (
              <div key={i} className="flex items-center gap-4">
                <div className="h-4 w-32 rounded bg-surface-800 animate-pulse" />
                <div className="h-4 w-24 rounded bg-surface-800 animate-pulse" />
                <div className="h-5 w-16 rounded-full bg-surface-800 animate-pulse" />
                <div className="h-4 w-12 rounded bg-surface-800 animate-pulse" />
                <div className="h-4 w-12 rounded bg-surface-800 animate-pulse" />
                <div className="h-4 w-28 rounded bg-surface-800 animate-pulse" />
                <div className="h-4 w-16 rounded bg-surface-800 animate-pulse" />
              </div>
            ))}
          </div>
        ) : fetchError ? (
          /* Error state */
          <div className="flex flex-col items-center justify-center py-16 px-6">
            <AlertTriangle size={36} className="text-error mb-3" />
            <p className="text-sm text-surface-300 mb-1">{fetchError}</p>
            <button
              onClick={() => fetchSessions(selectedUserId, null)}
              className="btn-secondary text-xs mt-2"
            >
              Retry
            </button>
          </div>
        ) : sessions.length === 0 ? (
          /* Empty state */
          <div className="flex flex-col items-center justify-center py-16 px-6">
            <MessageSquare size={40} className="text-surface-600 mb-3" />
            <p className="text-sm text-surface-400 mb-1">
              No sessions found for this user.
            </p>
            <p className="text-xs text-surface-500 mb-4">
              Create a session to start extracting memory.
            </p>
            <button
              onClick={openCreateDialog}
              className="btn-secondary text-xs"
            >
              <Plus size={14} />
              Create Session
            </button>
          </div>
        ) : (
          <>
            {/* Table — horizontal scroll on small screens */}
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-surface-800">
                    <th className="text-left text-xs font-medium text-surface-400 px-4 py-3">
                      External ID
                    </th>
                    <th className="text-left text-xs font-medium text-surface-400 px-4 py-3">
                      User ID
                    </th>
                    <th className="text-left text-xs font-medium text-surface-400 px-4 py-3">
                      Status
                    </th>
                    <th className="text-center text-xs font-medium text-surface-400 px-4 py-3">
                      Messages
                    </th>
                    <th className="text-center text-xs font-medium text-surface-400 px-4 py-3">
                      Facts
                    </th>
                    <th className="text-left text-xs font-medium text-surface-400 px-4 py-3">
                      Created
                    </th>
                    <th className="text-right text-xs font-medium text-surface-400 px-4 py-3">
                      Actions
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-surface-800">
                  {sessions.map((session) => (
                    <tr
                      key={session.id}
                      className="transition-colors hover:bg-surface-800/50"
                    >
                      {/* External ID */}
                      <td className="px-4 py-3 text-surface-100 font-medium">
                        {session.external_id}
                      </td>

                      {/* User ID — monospace truncated */}
                      <td className="px-4 py-3">
                        <span
                          className="font-mono text-xs text-surface-400 block max-w-[100px] truncate"
                          title={session.user_id}
                        >
                          {session.user_id}
                        </span>
                      </td>

                      {/* Status chip */}
                      <td className="px-4 py-3">
                        {renderStatusChip(session)}
                      </td>

                      {/* Messages count */}
                      <td className="px-4 py-3 text-center text-surface-300">
                        {session.message_count}
                      </td>

                      {/* Facts count */}
                      <td className="px-4 py-3 text-center text-surface-300">
                        {session.fact_count}
                      </td>

                      {/* Created date */}
                      <td className="px-4 py-3 text-surface-400 whitespace-nowrap">
                        {formatDate(session.created_at)}
                      </td>

                      {/* Actions */}
                      <td className="px-4 py-3 text-right">
                        <div className="flex items-center justify-end gap-1">
                          <button
                            onClick={() =>
                              router.push(
                                `/sessions/${session.id}?userId=${session.user_id}`,
                              )
                            }
                            className="btn-ghost p-1.5"
                            title="View session"
                          >
                            <Eye size={15} />
                          </button>
                          <button
                            onClick={() => openDeleteDialog(session)}
                            className="btn-ghost p-1.5 text-surface-400 hover:text-error"
                            title="Delete session"
                          >
                            <Trash2 size={15} />
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Load More */}
            {hasMore && (
              <div className="flex justify-center py-4 border-t border-surface-800">
                <button
                  onClick={() => fetchSessions(selectedUserId, cursor)}
                  disabled={loadingMore}
                  className="btn-secondary text-xs"
                >
                  {loadingMore ? (
                    <>
                      <Loader2 size={14} className="animate-spin" />
                      Loading…
                    </>
                  ) : (
                    "Load More"
                  )}
                </button>
              </div>
            )}
          </>
        )}
      </div>

      {/* ═══ Create Session Dialog ═══ */}
      {showCreateDialog && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          {/* Backdrop */}
          <div
            className="fixed inset-0 bg-black/60"
            onClick={() => !creating && setShowCreateDialog(false)}
          />

          {/* Dialog */}
          <div className="relative z-10 card-base w-full max-w-md mx-4 animate-slide-up">
            {/* Header */}
            <div className="flex items-center justify-between px-6 py-4 border-b border-surface-800">
              <h2 className="text-base font-semibold">Create Session</h2>
              <button
                onClick={() => !creating && setShowCreateDialog(false)}
                className="text-surface-400 hover:text-surface-200 transition-colors"
                disabled={creating}
              >
                <X size={18} />
              </button>
            </div>

            {/* Body */}
            <div className="p-6 space-y-4">
              {/* External ID */}
              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1.5">
                  External ID <span className="text-error">*</span>
                </label>
                <input
                  type="text"
                  value={newExternalId}
                  onChange={(e) => setNewExternalId(e.target.value)}
                  placeholder="e.g., conversation-123"
                  className="input-base"
                  autoFocus
                  disabled={creating}
                />
                <p className="text-xs text-surface-500 mt-1">
                  A unique identifier for this session in your system.
                </p>
              </div>

              {/* User selector */}
              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1.5">
                  User
                </label>
                <select
                  value={newUserId}
                  onChange={(e) => setNewUserId(e.target.value)}
                  className="input-base appearance-none cursor-pointer"
                  disabled={creating}
                >
                  {users.map((user) => (
                    <option key={user.id} value={user.id}>
                      {getUserLabel(user)}
                    </option>
                  ))}
                </select>
              </div>

              {/* Error */}
              {createError && (
                <div className="rounded-md border border-error/20 bg-error/10 px-3 py-2 text-sm text-error">
                  {createError}
                </div>
              )}
            </div>

            {/* Footer */}
            <div className="flex items-center justify-end gap-3 px-6 py-4 border-t border-surface-800">
              <button
                onClick={() => setShowCreateDialog(false)}
                className="btn-secondary text-sm"
                disabled={creating}
              >
                Cancel
              </button>
              <button
                onClick={handleCreateSession}
                disabled={creating || !newExternalId.trim()}
                className="btn-primary text-sm"
              >
                {creating ? (
                  <>
                    <Loader2 size={14} className="animate-spin" />
                    Creating…
                  </>
                ) : (
                  "Create"
                )}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ═══ Delete Confirmation Dialog ═══ */}
      {showDeleteDialog && deleteTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          {/* Backdrop */}
          <div
            className="fixed inset-0 bg-black/60"
            onClick={() => !deleting && setShowDeleteDialog(false)}
          />

          {/* Dialog */}
          <div className="relative z-10 card-base w-full max-w-sm mx-4 animate-slide-up">
            {/* Header */}
            <div className="flex items-center gap-3 px-6 py-4 border-b border-surface-800">
              <div className="flex h-10 w-10 items-center justify-center rounded-full bg-error/10 shrink-0">
                <AlertTriangle size={20} className="text-error" />
              </div>
              <div>
                <h2 className="text-base font-semibold">Delete Session</h2>
                <p className="text-xs text-surface-400">
                  This action cannot be undone.
                </p>
              </div>
            </div>

            {/* Body */}
            <div className="p-6">
              <p className="text-sm text-surface-300">
                Are you sure you want to delete session{" "}
                <span className="font-mono text-surface-100">
                  {deleteTarget.external_id}
                </span>
                ? All associated messages, facts, and extracted data will be
                permanently removed.
              </p>
            </div>

            {/* Footer */}
            <div className="flex items-center justify-end gap-3 px-6 py-4 border-t border-surface-800">
              <button
                onClick={() => setShowDeleteDialog(false)}
                className="btn-secondary text-sm"
                disabled={deleting}
              >
                Cancel
              </button>
              <button
                onClick={handleDeleteSession}
                disabled={deleting}
                className="btn-danger text-sm"
              >
                {deleting ? (
                  <>
                    <Loader2 size={14} className="animate-spin" />
                    Deleting…
                  </>
                ) : (
                  <>
                    <Trash2 size={14} />
                    Delete
                  </>
                )}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  </RequireAuth>
  );
}
