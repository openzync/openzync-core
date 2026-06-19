"use client";
import { RequireAuth } from "../../../../require-auth";

import { useEffect, useState, useCallback } from "react";
import {
  Plus,
  Key,
  X,
  Copy,
  CheckCircle,
  AlertCircle,
  Ban,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { toast } from "sonner";
import { get, post, del, ApiError } from "@/lib/api-client";
import { timeAgo, formatDate, copyToClipboard } from "@/lib/utils";
import { useProject } from "@/stores/project-context";
import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/empty-state";
import { ErrorState } from "@/components/shared/error-state";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { TableSkeleton } from "@/components/shared/skeleton";

// ─── Types ─────────────────────────────────────────────────────────────────────

interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  project_id: string;
  scopes: string[];
  is_revoked: boolean;
  last_used_at: string | null;
  created_at: string;
}

interface ApiKeyCreateResponse {
  id: string;
  name: string;
  prefix: string;
  project_id: string;
  raw_key: string;
  message: string;
}

// ─── Page ──────────────────────────────────────────────────────────────────────

export default function ProjectApiKeysPage() {
  const { project, loading: projectLoading } = useProject();
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Create dialog
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const [createdKey, setCreatedKey] = useState<ApiKeyCreateResponse | null>(null);
  const [showRawKey, setShowRawKey] = useState(false);
  const [copied, setCopied] = useState(false);

  // Revoke dialog
  const [revokeTarget, setRevokeTarget] = useState<ApiKey | null>(null);
  const [revoking, setRevoking] = useState(false);

  // ── Fetch ──────────────────────────────────────────────────────────────────

  const fetchKeys = useCallback(async () => {
    if (!project?.id) return;
    setLoading(true);
    setError(null);
    try {
      const data = await get<{ data: ApiKey[] }>(
        `/v1/projects/${project.id}/api-keys`
      );
      setKeys(data.data ?? []);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load API keys");
    } finally {
      setLoading(false);
    }
  }, [project?.id]);

  useEffect(() => {
    if (project?.id) fetchKeys();
  }, [project?.id, fetchKeys]);

  // ── Create key ─────────────────────────────────────────────────────────────

  const handleCreate = async () => {
    if (!newName.trim() || !project?.id) return;
    setCreating(true);
    try {
      const result = await post<ApiKeyCreateResponse>(
        `/v1/projects/${project.id}/api-keys`,
        { name: newName.trim() }
      );
      setCreatedKey(result);
      toast.success("API key created");
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Failed to create key";
      setError(msg);
      toast.error(msg);
      setCreating(false);
    }
  };

  const closeCreate = () => {
    setShowCreate(false);
    setNewName("");
    setCreating(false);
    setCreatedKey(null);
    setShowRawKey(false);
    setCopied(false);
    fetchKeys();
  };

  // ── Revoke key ─────────────────────────────────────────────────────────────

  const handleRevoke = async () => {
    if (!revokeTarget || !project?.id) return;
    setRevoking(true);
    try {
      await del(`/v1/projects/${project.id}/api-keys/${revokeTarget.id}`);
      setRevokeTarget(null);
      toast.success("API key revoked");
      fetchKeys();
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Failed to revoke key";
      setError(msg);
      toast.error(msg);
    } finally {
      setRevoking(false);
    }
  };

  // ── Copy raw key ───────────────────────────────────────────────────────────

  const handleCopy = async () => {
    if (!createdKey) return;
    const ok = await copyToClipboard(createdKey.raw_key);
    if (ok) {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
    <RequireAuth>
    <div className="space-y-6">
      <PageHeader
        title="API Keys"
        description={
          project
            ? `Manage API keys for programmatic access to ${project.name}`
            : "Manage API keys for programmatic access"
        }
        actions={
          <Button variant="primary" size="sm" icon={<Plus size={14} />} onClick={() => setShowCreate(true)}>
            Create Key
          </Button>
        }
      />

      {/* Loading state (project not yet loaded) */}
      {projectLoading && (
        <div className="card-base overflow-hidden">
          <TableSkeleton rows={4} cols={7} colWidths={["w-32", "w-20", "w-28", "w-20", "w-24", "w-16", "w-16"]} />
        </div>
      )}

      {/* Error */}
      {error && <ErrorState message={error} onRetry={fetchKeys} />}

      {/* Table */}
      {!projectLoading && (
        <div className="card-base overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-surface-800">
                  <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Name</th>
                  <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Prefix</th>
                  <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Scopes</th>
                  <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Last Used</th>
                  <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Created</th>
                  <th className="px-4 py-3 text-center text-xs font-medium uppercase tracking-wider text-surface-400">Status</th>
                  <th className="px-4 py-3 text-center text-xs font-medium uppercase tracking-wider text-surface-400 w-20">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-surface-800">
                {loading ? (
                  <TableSkeleton rows={4} cols={7} colWidths={["w-32", "w-20", "w-28", "w-20", "w-24", "w-16", "w-16"]} />
                ) : keys.length === 0 ? (
                  <tr>
                    <td colSpan={7}>
                      <EmptyState
                        icon={Key}
                        title="No API keys yet"
                        description="Create an API key to enable programmatic access to this project"
                        action={<Button variant="primary" size="sm" icon={<Plus size={14} />} onClick={() => setShowCreate(true)}>Create Key</Button>}
                      />
                    </td>
                  </tr>
                ) : (
                  keys.map((key, idx) => (
                    <tr
                      key={key.id}
                      className={cn("transition-colors hover:bg-surface-800/50", idx % 2 === 0 ? "bg-surface-950/50" : "")}
                    >
                      <td className="px-4 py-3">
                        <span className="text-surface-200 font-medium">{key.name}</span>
                      </td>
                      <td className="px-4 py-3">
                        <code className="text-xs bg-surface-800 text-surface-300 px-1.5 py-0.5 rounded font-mono">{key.prefix}...</code>
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex flex-wrap gap-1">
                          {key.scopes.length === 0 ? (
                            <span className="text-surface-500 text-xs">—</span>
                          ) : (
                            key.scopes.map((scope) => (
                              <Badge key={scope} variant="info" size="sm">{scope}</Badge>
                            ))
                          )}
                        </div>
                      </td>
                      <td className="px-4 py-3">
                        <span className="text-surface-400 text-xs">{timeAgo(key.last_used_at)}</span>
                      </td>
                      <td className="px-4 py-3">
                        <span className="text-surface-400 text-xs">{formatDate(key.created_at)}</span>
                      </td>
                      <td className="px-4 py-3 text-center">
                        <Badge variant={key.is_revoked ? "error" : "success"} size="sm">
                          {key.is_revoked ? "Revoked" : "Active"}
                        </Badge>
                      </td>
                      <td className="px-4 py-3 text-center">
                        {!key.is_revoked && (
                          <button
                            onClick={() => setRevokeTarget(key)}
                            className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-error"
                            title="Revoke key"
                          >
                            <Ban size={14} />
                          </button>
                        )}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* ── Create Dialog ──────────────────────────────────────────────────────── */}
      {showCreate && (
        <>
          <div className="fixed inset-0 z-50 bg-black/50" onClick={closeCreate} />
          <div className="fixed left-1/2 top-1/2 z-50 w-full max-w-md -translate-x-1/2 -translate-y-1/2 rounded-lg border border-surface-800 bg-surface-900 p-6 shadow-xl shadow-black/40 animate-slide-up">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-semibold text-[#F2F2F2]">
                {createdKey ? "Key Created" : "Create API Key"}
              </h3>
              <button onClick={closeCreate} className="text-surface-400 hover:text-white">
                <X size={18} />
              </button>
            </div>

            {!createdKey ? (
              <>
                <label className="block text-sm font-medium text-surface-300 mb-1.5">Key Name</label>
                <input
                  className="input-base"
                  placeholder="e.g. Production CI"
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && handleCreate()}
                  autoFocus
                />
                <div className="flex justify-end gap-3 mt-6">
                  <Button variant="secondary" size="sm" onClick={closeCreate}>Cancel</Button>
                  <Button variant="primary" size="sm" onClick={handleCreate} loading={creating} disabled={!newName.trim()}>
                    Create
                  </Button>
                </div>
              </>
            ) : (
              <>
                <p className="text-sm text-surface-400 mb-4">
                  {createdKey.message}
                </p>
                <div className="rounded-lg border border-surface-700 bg-surface-950 p-4">
                  <div className="flex items-center justify-between mb-2">
                    <span className="text-xs text-surface-500 font-medium uppercase tracking-wider">Your API Key</span>
                    <button
                      onClick={handleCopy}
                      className="btn-ghost p-1 text-surface-400 hover:text-white"
                      title="Copy to clipboard"
                    >
                      {copied ? <CheckCircle size={14} className="text-success" /> : <Copy size={14} />}
                    </button>
                  </div>
                  <code className="block text-sm font-mono text-accent-300 break-all select-all">
                    {showRawKey ? createdKey.raw_key : createdKey.raw_key.slice(0, 20) + "••••"}
                  </code>
                  <button
                    onClick={() => setShowRawKey(!showRawKey)}
                    className="text-xs text-accent-300 hover:text-accent-200 mt-2"
                  >
                    {showRawKey ? "Hide" : "Show full key"}
                  </button>
                </div>
                <p className="text-xs text-warning mt-3 flex items-center gap-1.5">
                  <AlertCircle size={12} />
                  This key will not be shown again. Copy it now.
                </p>
                <div className="flex justify-end mt-4">
                  <Button variant="primary" size="sm" onClick={closeCreate}>Done</Button>
                </div>
              </>
            )}
          </div>
        </>
      )}

      {/* ── Revoke Confirm Dialog ──────────────────────────────────────────────── */}
      <ConfirmDialog
        open={!!revokeTarget}
        title="Revoke API Key"
        message={`Are you sure you want to revoke "${revokeTarget?.name}"? This action cannot be undone.`}
        confirmLabel="Revoke"
        variant="danger"
        loading={revoking}
        onConfirm={handleRevoke}
        onCancel={() => setRevokeTarget(null)}
      />
    </div>
  </RequireAuth>
  );
}
