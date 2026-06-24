"use client";
import { RequireAuth } from "../../../require-auth";

import { useState, useEffect, useCallback } from "react";
import { useRouter } from "next/navigation";
import {
  Plus,
  Eye,
  Trash2,
  X,
  AlertTriangle,
  MessageSquare,
} from "lucide-react";
import { get, post, del as apiDel, ApiError } from "@/lib/api-client";
import { formatDate } from "@/lib/utils";
import { useProject } from "@/stores/project-context";
import { toast } from "sonner";
import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/empty-state";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { TableSkeleton } from "@/components/shared/skeleton";

// ─── Types ─────────────────────────────────────────────────────────────────────

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

// ─── Page ──────────────────────────────────────────────────────────────────────

export default function ProjectSessionsPage() {
  const router = useRouter();
  const { project, loading: projectLoading } = useProject();
  const projectId = project?.id;

  const [sessions, setSessions] = useState<Session[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [fetchError, setFetchError] = useState("");

  // Create dialog
  const [showCreateDialog, setShowCreateDialog] = useState(false);
  const [newExternalId, setNewExternalId] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState("");

  // Delete dialog
  const [deleteTarget, setDeleteTarget] = useState<Session | null>(null);
  const [deleting, setDeleting] = useState(false);

  // ── Fetch sessions ────────────────────────────────────────────────────────

  const fetchSessions = useCallback(
    async (cursorVal: string | null) => {
      if (!projectId) return;
      const isInitial = !cursorVal;
      if (isInitial) { setLoading(true); setFetchError(""); }
      else { setLoadingMore(true); }

      try {
        let url = `/v1/projects/${projectId}/sessions?limit=50&include_closed=true`;
        if (cursorVal) url += `&cursor=${encodeURIComponent(cursorVal)}`;
        const json = await get<SessionsApiResponse>(url);
        const items = json.data ?? [];
        if (isInitial) { setSessions(items); }
        else { setSessions((prev) => [...prev, ...items]); }
        setCursor(json.next_cursor ?? null);
        setHasMore(json.has_more ?? false);
      } catch (err) {
        if (isInitial) {
          setFetchError(
            err instanceof ApiError ? err.message : "Network error loading sessions",
          );
          setSessions([]);
        }
      } finally {
        setLoading(false);
        setLoadingMore(false);
      }
    },
    [projectId],
  );

  useEffect(() => {
    if (projectId) fetchSessions(null);
  }, [projectId, fetchSessions]);

  // ── Create session ────────────────────────────────────────────────────────

  async function handleCreateSession() {
    const trimmedId = newExternalId.trim();
    if (!trimmedId) { setCreateError("External ID is required"); return; }
    setCreating(true); setCreateError("");
    try {
      await post(`/v1/projects/${projectId}/sessions`, { external_id: trimmedId });
      setShowCreateDialog(false);
      setNewExternalId("");
      toast.success(`Session "${trimmedId}" created`);
      fetchSessions(null);
    } catch (err) {
      setCreateError(
        err instanceof ApiError ? err.message : "Network error. Please try again.",
      );
    } finally { setCreating(false); }
  }

  // ── Delete session ────────────────────────────────────────────────────────

  async function handleDeleteSession() {
    if (!deleteTarget || !projectId) return;
    setDeleting(true);
    try {
      await apiDel(`/v1/projects/${projectId}/sessions/${deleteTarget.id}`);
      setSessions((prev) => prev.filter((s) => s.id !== deleteTarget.id));
      setDeleteTarget(null);
      toast.success(`Session "${deleteTarget.external_id}" deleted`);
    } catch {
      toast.error("Failed to delete session");
    } finally { setDeleting(false); }
  }

  // ── Loading state ────────────────────────────────────────────────────────

  if (projectLoading) {
    return (
      <RequireAuth>
        <div className="space-y-6">
          <PageHeader title="Sessions" description="Loading project..." />
        </div>
      </RequireAuth>
    );
  }

  if (!projectId) {
    return (
      <RequireAuth>
        <div className="card-base p-12 flex flex-col items-center justify-center">
          <AlertTriangle size={36} className="text-error mb-3" />
          <p className="text-sm text-surface-300">Project not found</p>
        </div>
      </RequireAuth>
    );
  }

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
    <RequireAuth>
    <div className="space-y-6">
      <PageHeader
        title="Sessions"
        description={`Conversation sessions with extracted memory${project ? ` · ${project.name}` : ""}`}
        actions={
          <Button variant="primary" size="sm" icon={<Plus size={14} />} onClick={() => {
            setNewExternalId(""); setCreateError(""); setShowCreateDialog(true);
          }}>
            Create Session
          </Button>
        }
      />

      {/* Sessions table */}
      <div className="card-base overflow-hidden">
        {loading ? (
          <div className="p-6">
            <TableSkeleton rows={5} cols={6} colWidths={["w-32", "w-16", "w-12", "w-12", "w-28", "w-16"]} />
          </div>
        ) : fetchError ? (
          <div className="flex flex-col items-center justify-center py-16 px-6">
            <AlertTriangle size={36} className="text-error mb-3" />
            <p className="text-sm text-surface-300 mb-1">{fetchError}</p>
            <Button variant="secondary" size="sm" onClick={() => fetchSessions(null)}>Retry</Button>
          </div>
        ) : sessions.length === 0 ? (
          <EmptyState
            icon={MessageSquare}
            title="No sessions yet"
            description="Create a session to start extracting memory and knowledge."
            action={<Button variant="secondary" size="sm" icon={<Plus size={14} />} onClick={() => {
              setNewExternalId(""); setCreateError(""); setShowCreateDialog(true);
            }}>Create Session</Button>}
          />
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-surface-800">
                    <th className="text-left text-xs font-medium text-surface-400 px-4 py-3">External ID</th>
                    <th className="text-left text-xs font-medium text-surface-400 px-4 py-3">Status</th>
                    <th className="text-center text-xs font-medium text-surface-400 px-4 py-3">Messages</th>
                    <th className="text-center text-xs font-medium text-surface-400 px-4 py-3">Facts</th>
                    <th className="text-left text-xs font-medium text-surface-400 px-4 py-3">Created</th>
                    <th className="text-right text-xs font-medium text-surface-400 px-4 py-3">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-surface-800">
                  {sessions.map((session) => (
                    <tr key={session.id} className="transition-colors hover:bg-surface-800/50">
                      <td className="px-4 py-3 text-surface-100 font-medium">{session.external_id}</td>
                      <td className="px-4 py-3">
                        <Badge variant={session.is_active ? "success" : "default"} size="sm">
                          <span className={`mr-1.5 h-1.5 w-1.5 rounded-full inline-block ${session.is_active ? "bg-success" : "bg-surface-500"}`} />
                          {session.is_active ? "Active" : "Closed"}
                        </Badge>
                      </td>
                      <td className="px-4 py-3 text-center text-surface-300">{session.message_count}</td>
                      <td className="px-4 py-3 text-center text-surface-300">{session.fact_count}</td>
                      <td className="px-4 py-3 text-surface-400 whitespace-nowrap">{formatDate(session.created_at)}</td>
                      <td className="px-4 py-3 text-right">
                        <div className="flex items-center justify-end gap-1">
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => router.push(`/projects/${projectId}/sessions/${session.id}`)}
                            className="p-1.5" title="View session"><Eye size={15} /></Button>
                          <Button variant="ghost" size="sm" onClick={() => setDeleteTarget(session)}
                            className="text-surface-400 hover:text-error" title="Delete session"><Trash2 size={15} /></Button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {hasMore && (
              <div className="flex justify-center py-4 border-t border-surface-800">
                <Button variant="secondary" size="sm" onClick={() => fetchSessions(cursor)} loading={loadingMore}>
                  Load More
                </Button>
              </div>
            )}
          </>
        )}
      </div>

      {/* ── Create Session Dialog ─────────────────────────────────────────────── */}
      {showCreateDialog && (
        <>
          <div className="fixed inset-0 z-50 bg-black/60" onClick={() => !creating && setShowCreateDialog(false)} />
          <div className="fixed left-1/2 top-1/2 z-50 w-full max-w-md -translate-x-1/2 -translate-y-1/2 card-base animate-slide-up">
            <div className="flex items-center justify-between px-6 py-4 border-b border-surface-800">
              <h2 className="text-base font-semibold text-text-primary">Create Session</h2>
              <button onClick={() => !creating && setShowCreateDialog(false)} className="text-surface-400 hover:text-surface-200" disabled={creating}>
                <X size={18} />
              </button>
            </div>
            <div className="p-6 space-y-4">
              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1.5">External ID <span className="text-error">*</span></label>
                <input type="text" value={newExternalId} onChange={(e) => setNewExternalId(e.target.value)}
                  placeholder="e.g., conversation-123" className="input-base" autoFocus disabled={creating} />
                <p className="text-xs text-surface-500 mt-1">A unique identifier for this session within the project.</p>
              </div>
              {createError && (
                <div className="rounded-md border border-error/20 bg-error/10 px-3 py-2 text-sm text-error">{createError}</div>
              )}
            </div>
            <div className="flex items-center justify-end gap-3 px-6 py-4 border-t border-surface-800">
              <Button variant="secondary" size="sm" onClick={() => setShowCreateDialog(false)} disabled={creating}>Cancel</Button>
              <Button variant="primary" size="sm" onClick={handleCreateSession} loading={creating} disabled={!newExternalId.trim()}>Create</Button>
            </div>
          </div>
        </>
      )}

      {/* ── Delete Confirm Dialog ─────────────────────────────────────────────── */}
      <ConfirmDialog
        open={!!deleteTarget}
        title="Delete Session"
        message={`Are you sure you want to delete session "${deleteTarget?.external_id}"? All associated messages, facts, and extracted data will be permanently removed.`}
        confirmLabel="Delete"
        variant="danger"
        loading={deleting}
        onConfirm={handleDeleteSession}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  </RequireAuth>
  );
}
