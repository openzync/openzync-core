"use client";

import { useEffect, useState, useCallback } from "react";
import {
  Plus,
  Key,
  X,
  Copy,
  Trash2,
  CheckCircle,
  AlertCircle,
  Loader2,
  Ban,
} from "lucide-react";
import { cn } from "@/lib/utils";

// ─── Types ─────────────────────────────────────────────────────────────────────

interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  is_revoked: boolean;
  last_used_at: string | null;
  created_at: string;
}

interface ApiKeyCreateResponse {
  id: string;
  name: string;
  prefix: string;
  raw_key: string;
  message: string;
}

interface ApiKeysResponse {
  data: ApiKey[];
}

interface ToastState {
  visible: boolean;
  message: string;
  type: "success" | "error";
}

// ─── Constants ─────────────────────────────────────────────────────────────────

const API_BASE = "http://localhost:8000";

// ─── Helpers ───────────────────────────────────────────────────────────────────

function authHeaders(): Record<string, string> {
  const token = sessionStorage.getItem("mg_access_token");
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  return headers;
}

function formatDate(dateStr: string): string {
  const d = new Date(dateStr);
  return d.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function timeAgo(dateStr: string | null): string {
  if (!dateStr) return "—";
  const seconds = Math.floor((Date.now() - new Date(dateStr).getTime()) / 1000);
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const mins = Math.floor(seconds / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

// ─── Spinner ───────────────────────────────────────────────────────────────────

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

// ─── Toast ─────────────────────────────────────────────────────────────────────

const TOAST_DURATION = 4000;

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

// ─── Create Dialog ─────────────────────────────────────────────────────────────

interface CreateDialogProps {
  onClose: () => void;
  onCreate: (name: string) => Promise<void>;
}

function CreateDialog({ onClose, onCreate }: CreateDialogProps) {
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [createdKey, setCreatedKey] = useState<ApiKeyCreateResponse | null>(null);
  const [copied, setCopied] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) {
      setError("Key name is required");
      return;
    }
    setCreating(true);
    setError(null);
    try {
      await onCreate(trimmed);
    } catch {
      // Error handled by caller
    } finally {
      setCreating(false);
    }
  };

  // When a parent passes createdKey via a different mechanism, we won't use it.
  // The create function sets the createdKey in the parent and closes this dialog.
  // Re-reading: the spec says the dialog shows the raw key ONCE then closes.
  // We need the parent to set a "createdKey" state, then this dialog shows it.
  // Let's redesign: parent passes `createdKey` as a prop.

  const handleCopyKey = async () => {
    if (!createdKey) return;
    try {
      await navigator.clipboard.writeText(createdKey.raw_key);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setError("Failed to copy to clipboard");
    }
  };

  // If we have a created key, show the one-time display
  if (createdKey) {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 animate-fade-in" onClick={onClose}>
        <div
          className="card-base w-full max-w-lg p-6 shadow-xl shadow-black/40 animate-slide-up"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold">Key Created</h2>
            <button onClick={onClose} className="btn-ghost p-1 rounded-md text-surface-400 hover:text-white">
              <X size={18} />
            </button>
          </div>

          <p className="text-sm text-surface-400 mb-4">
            Copy this key now. You won&apos;t be able to see it again.
          </p>

          {/* Terminal-style key display */}
          <div className="bg-surface-950 border border-surface-700 font-mono text-sm p-4 rounded relative">
            <code className="text-accent-300 break-all">{createdKey.raw_key}</code>
            <button
              onClick={handleCopyKey}
              className={cn(
                "absolute top-2 right-2 btn-ghost p-1.5 rounded text-xs",
                copied ? "text-success" : "text-surface-400 hover:text-white",
              )}
            >
              {copied ? (
                <span className="flex items-center gap-1">
                  <CheckCircle size={14} /> Copied
                </span>
              ) : (
                <span className="flex items-center gap-1">
                  <Copy size={14} /> Copy
                </span>
              )}
            </button>
          </div>

          <p className="text-xs text-surface-500 mt-3">
            Prefix: <span className="font-mono text-surface-300">{createdKey.prefix}</span>
          </p>

          <div className="flex justify-end mt-4">
            <button onClick={onClose} className="btn-primary text-sm">
              Done
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 animate-fade-in" onClick={onClose}>
      <div
        className="card-base w-full max-w-md p-6 shadow-xl shadow-black/40 animate-slide-up"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">Create API Key</h2>
          <button onClick={onClose} className="btn-ghost p-1 rounded-md text-surface-400 hover:text-white">
            <X size={18} />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">
              Key Name <span className="text-error">*</span>
            </label>
            <input
              className="input-base"
              placeholder="e.g. Production CI"
              value={name}
              onChange={(e) => {
                setName(e.target.value);
                if (error) setError(null);
              }}
              autoFocus
              disabled={creating}
            />
            <p className="text-xs text-surface-500 mt-1">A human-readable label for this API key.</p>
          </div>

          {error && (
            <div className="rounded-md bg-error/10 border border-error/30 px-3 py-2 text-sm text-error flex items-center gap-2">
              <AlertCircle size={14} />
              {error}
            </div>
          )}

          <div className="flex justify-end gap-3 pt-2">
            <button type="button" onClick={onClose} className="btn-secondary text-sm" disabled={creating}>
              Cancel
            </button>
            <button type="submit" disabled={creating} className="btn-primary text-sm min-w-[100px] justify-center">
              {creating ? (
                <span className="flex items-center gap-2">
                  <Spinner /> Creating...
                </span>
              ) : (
                "Create Key"
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ─── Revoke Dialog ─────────────────────────────────────────────────────────────

interface RevokeDialogProps {
  keyName: string;
  onClose: () => void;
  onConfirm: () => Promise<void>;
}

function RevokeDialog({ keyName, onClose, onConfirm }: RevokeDialogProps) {
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
            <Ban size={18} className="text-error" />
          </div>
          <div>
            <h2 className="text-lg font-semibold">Revoke API Key</h2>
            <p className="text-sm text-surface-400">This action cannot be undone.</p>
          </div>
        </div>

        <p className="text-sm text-surface-300 mb-2">
          Are you sure you want to revoke key
        </p>
        <p className="text-sm font-medium text-white mb-5">
          &ldquo;{keyName}&rdquo;
        </p>
        <p className="text-xs text-surface-500 mb-5">
          Any services using this key will immediately lose access.
        </p>

        <div className="flex justify-end gap-3">
          <button onClick={onClose} className="btn-secondary text-sm" disabled={submitting}>
            Cancel
          </button>
          <button onClick={handleConfirm} disabled={submitting} className="btn-danger text-sm min-w-[100px] justify-center">
            {submitting ? (
              <span className="flex items-center gap-2">
                <Spinner /> Revoking...
              </span>
            ) : (
              <span className="flex items-center gap-2">
                <Ban size={14} /> Revoke
              </span>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Main Page ─────────────────────────────────────────────────────────────────

export default function ApiKeysPage() {
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [revokeTarget, setRevokeTarget] = useState<ApiKey | null>(null);
  const [createdKey, setCreatedKey] = useState<ApiKeyCreateResponse | null>(null);

  const [toast, setToast] = useState<ToastState>({ visible: false, message: "", type: "success" });

  const showToast = useCallback((message: string, type: "success" | "error") => {
    setToast({ visible: true, message, type });
  }, []);

  const dismissToast = useCallback(() => {
    setToast((prev) => ({ ...prev, visible: false }));
  }, []);

  // ── Fetch ──────────────────────────────────────────────────────────────────

  const fetchKeys = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/v1/admin/api-keys`, { headers: authHeaders() });
      if (!res.ok) throw new Error("Failed to load API keys");
      const data: ApiKeysResponse = await res.json();
      setKeys(data.data ?? []);
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Failed to load API keys", "error");
    } finally {
      setLoading(false);
    }
  }, [showToast]);

  useEffect(() => {
    fetchKeys();
  }, [fetchKeys]);

  // ── Create ─────────────────────────────────────────────────────────────────

  const handleCreate = async (name: string) => {
    const res = await fetch(`${API_BASE}/v1/admin/api-keys`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ name }),
    });

    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail ?? "Failed to create API key");
    }

    const data: ApiKeyCreateResponse = await res.json();
    setCreatedKey(data);
    showToast(`Key "${name}" created successfully`, "success");
    await fetchKeys();
  };

  // ── Revoke ─────────────────────────────────────────────────────────────────

  const handleRevoke = async () => {
    if (!revokeTarget) return;

    const res = await fetch(`${API_BASE}/v1/admin/api-keys/${revokeTarget.id}`, {
      method: "DELETE",
      headers: authHeaders(),
    });

    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail ?? "Failed to revoke API key");
    }

    setRevokeTarget(null);
    showToast(`Key "${revokeTarget.name}" revoked`, "success");
    setKeys((prev) => prev.map((k) => (k.id === revokeTarget.id ? { ...k, is_revoked: true } : k)));
  };

  // ── Skeleton rows ──────────────────────────────────────────────────────────

  const skeletonRows = Array.from({ length: 5 }, (_, i) => (
    <tr key={`skel-${i}`} className={i % 2 === 0 ? "bg-surface-950/50" : ""}>
      {[1, 2, 3, 4, 5].map((col) => (
        <td key={col} className="px-4 py-3">
          <div className="h-4 rounded bg-surface-800 animate-pulse" style={{ width: col === 2 ? "120px" : "80px" }} />
        </td>
      ))}
    </tr>
  ));

  // ── Empty state ────────────────────────────────────────────────────────────

  const emptyRow = (
    <tr>
      <td colSpan={7}>
        <div className="flex flex-col items-center justify-center py-16 text-surface-500">
          <Key size={40} className="mb-3 opacity-40" />
          <p className="text-sm font-medium">No API keys found</p>
          <p className="text-xs mt-1">Create an API key to enable programmatic access</p>
        </div>
      </td>
    </tr>
  );

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">API Keys</h1>
          <p className="text-sm text-surface-400 mt-1">Manage API keys for programmatic access</p>
        </div>
        <button onClick={() => setShowCreate(true)} className="btn-primary text-sm">
          <Plus size={16} />
          Create Key
        </button>
      </div>

      {/* Table */}
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
                <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-surface-400">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-surface-800">
              {loading
                ? skeletonRows
                : keys.length === 0
                  ? emptyRow
                  : keys.map((key, idx) => (
                      <tr
                        key={key.id}
                        className={cn(
                          "transition-colors hover:bg-surface-800/50",
                          idx % 2 === 0 ? "bg-surface-950/50" : "",
                          key.is_revoked && "opacity-50",
                        )}
                      >
                        {/* Name */}
                        <td className="px-4 py-3">
                          <span className={cn("font-medium", key.is_revoked ? "text-surface-500" : "text-white")}>
                            {key.name}
                          </span>
                        </td>

                        {/* Prefix — monospace chip */}
                        <td className="px-4 py-3">
                          <span className="font-mono text-xs bg-surface-800 text-surface-300 px-2 py-1 rounded">
                            {key.prefix}
                          </span>
                        </td>

                        {/* Scopes */}
                        <td className="px-4 py-3">
                          <div className="flex flex-wrap gap-1">
                            {key.scopes.length > 0 ? (
                              key.scopes.map((scope) => (
                                <span
                                  key={scope}
                                  className="inline-flex items-center rounded-full bg-brand-500/10 text-brand-300 px-2 py-0.5 text-[11px] font-medium"
                                >
                                  {scope}
                                </span>
                              ))
                            ) : (
                              <span className="text-surface-500 text-xs italic">None</span>
                            )}
                          </div>
                        </td>

                        {/* Last used */}
                        <td className="px-4 py-3 text-surface-400 text-xs">
                          {timeAgo(key.last_used_at)}
                        </td>

                        {/* Created */}
                        <td className="px-4 py-3">
                          <div className="flex flex-col">
                            <span className="text-surface-300 text-xs">{formatDate(key.created_at)}</span>
                          </div>
                        </td>

                        {/* Status */}
                        <td className="px-4 py-3 text-center">
                          {key.is_revoked ? (
                            <span className="inline-flex items-center rounded-full bg-error/10 text-error px-2.5 py-0.5 text-xs font-medium">
                              Revoked
                            </span>
                          ) : (
                            <span className="inline-flex items-center rounded-full bg-success/10 text-success px-2.5 py-0.5 text-xs font-medium">
                              Active
                            </span>
                          )}
                        </td>

                        {/* Actions */}
                        <td className="px-4 py-3 text-right">
                          {!key.is_revoked ? (
                            <button
                              onClick={() => setRevokeTarget(key)}
                              className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-error"
                              title="Revoke key"
                            >
                              <Ban size={14} />
                            </button>
                          ) : (
                            <span className="text-surface-600 text-xs italic">—</span>
                          )}
                        </td>
                      </tr>
                    ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* ── Create Dialog ─────────────────────────────────────────────────────── */}
      {showCreate && !createdKey && (
        <CreateDialog
          onClose={() => {
            setShowCreate(false);
            setCreatedKey(null);
          }}
          onCreate={handleCreate}
        />
      )}

      {/* Show created key one-time display */}
      {createdKey && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 animate-fade-in" onClick={() => { setCreatedKey(null); setShowCreate(false); }}>
          <div
            className="card-base w-full max-w-lg p-6 shadow-xl shadow-black/40 animate-slide-up"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-lg font-semibold">Key Created</h2>
              <button onClick={() => { setCreatedKey(null); setShowCreate(false); }} className="btn-ghost p-1 rounded-md text-surface-400 hover:text-white">
                <X size={18} />
              </button>
            </div>

            <p className="text-sm text-surface-400 mb-4">
              Copy this key now. You won&apos;t be able to see it again.
            </p>

            {/* Terminal-style key display */}
            <div className="bg-surface-950 border border-surface-700 font-mono text-sm p-4 rounded relative">
              <code className="text-accent-300 break-all">{createdKey.raw_key}</code>
              <button
                onClick={async () => {
                  try {
                    await navigator.clipboard.writeText(createdKey.raw_key);
                    showToast("Key copied to clipboard", "success");
                  } catch {
                    showToast("Failed to copy", "error");
                  }
                }}
                className="absolute top-2 right-2 btn-ghost p-1.5 rounded text-xs text-surface-400 hover:text-white"
              >
                <Copy size={14} />
              </button>
            </div>

            <p className="text-xs text-surface-500 mt-3">
              Prefix: <span className="font-mono text-surface-300">{createdKey.prefix}</span>
            </p>

            <div className="flex justify-end mt-4">
              <button onClick={() => { setCreatedKey(null); setShowCreate(false); }} className="btn-primary text-sm">
                Done
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Revoke Dialog ─────────────────────────────────────────────────────── */}
      {revokeTarget && (
        <RevokeDialog
          keyName={revokeTarget.name}
          onClose={() => setRevokeTarget(null)}
          onConfirm={handleRevoke}
        />
      )}

      {/* ── Toast ─────────────────────────────────────────────────────────────── */}
      <Toast toast={toast} onDismiss={dismissToast} />
    </div>
  );
}
