"use client";
import { RequireAuth } from "../../require-auth";

import { useEffect, useState, useCallback } from "react";
import {
  Plus,
  Eye,
  Trash2,
  X,
  FileJson,
  CheckCircle,
  AlertCircle,
  Loader2,
} from "lucide-react";
import { cn } from "@/lib/utils";

// ─── Types ─────────────────────────────────────────────────────────────────────

interface Schema {
  id: string;
  name: string;
  type: string;
  json_schema: Record<string, unknown>;
  prompt_template: string | null;
  is_active: boolean;
  created_at: string;
}

interface SchemasResponse {
  data: Schema[];
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
  onCreate: (data: { name: string; type: string; json_schema: string; prompt_template: string }) => Promise<void>;
}

function CreateDialog({ onClose, onCreate }: CreateDialogProps) {
  const [form, setForm] = useState({
    name: "",
    type: "structured",
    json_schema: JSON.stringify({ type: "object", properties: {} }, null, 2),
    prompt_template: "",
  });
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleChange = (field: string, value: string) => {
    setForm((prev) => ({ ...prev, [field]: value }));
    if (error) setError(null);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!form.name.trim()) {
      setError("Schema name is required");
      return;
    }

    // Validate JSON
    try {
      JSON.parse(form.json_schema);
    } catch {
      setError("JSON schema is not valid JSON");
      return;
    }

    setSubmitting(true);
    try {
      await onCreate(form);
    } catch {
      // Error handled by caller
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 animate-fade-in" onClick={onClose}>
      <div
        className="card-base w-full max-w-2xl p-6 shadow-xl shadow-black/40 animate-slide-up max-h-[90vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">Create Schema</h2>
          <button onClick={onClose} className="btn-ghost p-1 rounded-md text-surface-400 hover:text-white">
            <X size={18} />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          {/* Name */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">
              Name <span className="text-error">*</span>
            </label>
            <input
              className="input-base"
              placeholder="e.g. person"
              value={form.name}
              onChange={(e) => handleChange("name", e.target.value)}
              autoFocus
              disabled={submitting}
            />
          </div>

          {/* Type */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Type</label>
            <select
              className="input-base appearance-none cursor-pointer"
              value={form.type}
              onChange={(e) => handleChange("type", e.target.value)}
              disabled={submitting}
            >
              <option value="structured">structured</option>
              <option value="classification">classification</option>
            </select>
          </div>

          {/* JSON Schema */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">JSON Schema</label>
            <textarea
              className="input-base min-h-[150px] py-2 font-mono text-xs resize-y leading-relaxed"
              style={{ height: "auto" }}
              value={form.json_schema}
              onChange={(e) => handleChange("json_schema", e.target.value)}
              disabled={submitting}
              rows={8}
            />
            <p className="text-xs text-surface-500 mt-1">JSON Schema definition for validation.</p>
          </div>

          {/* Prompt Template */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Prompt Template</label>
            <textarea
              className="input-base min-h-[100px] py-2 font-mono text-xs resize-y leading-relaxed"
              value={form.prompt_template}
              onChange={(e) => handleChange("prompt_template", e.target.value)}
              disabled={submitting}
              placeholder="Optional prompt template for LLM extraction..."
              rows={6}
            />
            <p className="text-xs text-surface-500 mt-1">Optional. Used to guide LLM extraction.</p>
          </div>

          {/* Error */}
          {error && (
            <div className="rounded-md bg-error/10 border border-error/30 px-3 py-2 text-sm text-error flex items-center gap-2">
              <AlertCircle size={14} />
              {error}
            </div>
          )}

          {/* Actions */}
          <div className="flex justify-end gap-3 pt-2">
            <button type="button" onClick={onClose} className="btn-secondary text-sm" disabled={submitting}>
              Cancel
            </button>
            <button type="submit" disabled={submitting} className="btn-primary text-sm min-w-[120px] justify-center">
              {submitting ? (
                <span className="flex items-center gap-2">
                  <Spinner /> Creating...
                </span>
              ) : (
                "Create Schema"
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ─── View Dialog ───────────────────────────────────────────────────────────────

interface ViewDialogProps {
  schema: Schema;
  onClose: () => void;
}

function ViewDialog({ schema, onClose }: ViewDialogProps) {
  const formattedJson = JSON.stringify(schema.json_schema, null, 2);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 animate-fade-in" onClick={onClose}>
      <div
        className="card-base w-full max-w-2xl p-6 shadow-xl shadow-black/40 animate-slide-up max-h-[90vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">{schema.name}</h2>
          <button onClick={onClose} className="btn-ghost p-1 rounded-md text-surface-400 hover:text-white">
            <X size={18} />
          </button>
        </div>

        {/* Metadata */}
        <div className="grid grid-cols-2 gap-4 mb-4">
          <div>
            <span className="text-xs text-surface-500 block">Type</span>
            <span className="text-sm text-surface-200 capitalize">{schema.type}</span>
          </div>
          <div>
            <span className="text-xs text-surface-500 block">Created</span>
            <span className="text-sm text-surface-200">{formatDate(schema.created_at)}</span>
          </div>
          <div>
            <span className="text-xs text-surface-500 block">Status</span>
            {schema.is_active ? (
              <span className="inline-flex items-center rounded-full bg-success/10 text-success px-2.5 py-0.5 text-xs font-medium">
                Active
              </span>
            ) : (
              <span className="inline-flex items-center rounded-full bg-surface-700 text-surface-400 px-2.5 py-0.5 text-xs font-medium">
                Inactive
              </span>
            )}
          </div>
          <div>
            <span className="text-xs text-surface-500 block">ID</span>
            <span className="font-mono text-xs text-surface-400">{schema.id}</span>
          </div>
        </div>

        {/* JSON Schema */}
        <div className="mb-4">
          <span className="text-xs font-medium text-surface-400 block mb-1.5">JSON Schema</span>
          <div className="bg-surface-950 border border-surface-700 font-mono text-xs p-4 rounded overflow-x-auto max-h-64 overflow-y-auto">
            <pre className="text-surface-200 whitespace-pre">{formattedJson}</pre>
          </div>
        </div>

        {/* Prompt Template */}
        {schema.prompt_template && (
          <div>
            <span className="text-xs font-medium text-surface-400 block mb-1.5">Prompt Template</span>
            <div className="bg-surface-950 border border-surface-700 font-mono text-xs p-4 rounded overflow-x-auto max-h-40 overflow-y-auto">
              <pre className="text-surface-200 whitespace-pre-wrap">{schema.prompt_template}</pre>
            </div>
          </div>
        )}

        <div className="flex justify-end mt-4">
          <button onClick={onClose} className="btn-primary text-sm">
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Delete Dialog ─────────────────────────────────────────────────────────────

interface DeleteDialogProps {
  schemaName: string;
  onClose: () => void;
  onConfirm: () => Promise<void>;
}

function DeleteDialog({ schemaName, onClose, onConfirm }: DeleteDialogProps) {
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
            <h2 className="text-lg font-semibold">Delete Schema</h2>
            <p className="text-sm text-surface-400">This action cannot be undone.</p>
          </div>
        </div>

        <p className="text-sm text-surface-300 mb-2">
          Are you sure you want to delete schema
        </p>
        <p className="text-sm font-medium text-white mb-5">
          &ldquo;{schemaName}&rdquo;
        </p>
        <p className="text-xs text-surface-500 mb-5">
          This will permanently remove the schema definition and all associated extraction configurations.
        </p>

        <div className="flex justify-end gap-3">
          <button onClick={onClose} className="btn-secondary text-sm" disabled={submitting}>
            Cancel
          </button>
          <button onClick={handleConfirm} disabled={submitting} className="btn-danger text-sm min-w-[100px] justify-center">
            {submitting ? (
              <span className="flex items-center gap-2">
                <Spinner /> Deleting...
              </span>
            ) : (
              <span className="flex items-center gap-2">
                <Trash2 size={14} /> Delete
              </span>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Main Page ─────────────────────────────────────────────────────────────────

export default function SchemasPage() {
  const [schemas, setSchemas] = useState<Schema[]>([]);
  const [loading, setLoading] = useState(true);

  const [showCreate, setShowCreate] = useState(false);
  const [viewTarget, setViewTarget] = useState<Schema | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Schema | null>(null);

  const [toast, setToast] = useState<ToastState>({ visible: false, message: "", type: "success" });

  const showToast = useCallback((message: string, type: "success" | "error") => {
    setToast({ visible: true, message, type });
  }, []);

  const dismissToast = useCallback(() => {
    setToast((prev) => ({ ...prev, visible: false }));
  }, []);

  // ── Fetch ──────────────────────────────────────────────────────────────────

  const fetchSchemas = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/v1/admin/schemas`, { headers: authHeaders() });
      if (!res.ok) throw new Error("Failed to load schemas");
      const data: SchemasResponse = await res.json();
      setSchemas(data.data ?? []);
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Failed to load schemas", "error");
    } finally {
      setLoading(false);
    }
  }, [showToast]);

  useEffect(() => {
    fetchSchemas();
  }, [fetchSchemas]);

  // ── Create ─────────────────────────────────────────────────────────────────

  const handleCreate = async (data: { name: string; type: string; json_schema: string; prompt_template: string }) => {
    const payload: Record<string, unknown> = {
      name: data.name.trim(),
      type: data.type,
      json_schema: JSON.parse(data.json_schema),
    };
    if (data.prompt_template.trim()) payload.prompt_template = data.prompt_template.trim();

    const res = await fetch(`${API_BASE}/v1/admin/schemas`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify(payload),
    });

    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail ?? "Failed to create schema");
    }

    setShowCreate(false);
    showToast(`Schema "${data.name}" created successfully`, "success");
    await fetchSchemas();
  };

  // ── Delete ─────────────────────────────────────────────────────────────────

  const handleDelete = async () => {
    if (!deleteTarget) return;

    const res = await fetch(`${API_BASE}/v1/admin/schemas/${deleteTarget.id}`, {
      method: "DELETE",
      headers: authHeaders(),
    });

    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail ?? "Failed to delete schema");
    }

    setDeleteTarget(null);
    showToast(`Schema "${deleteTarget.name}" deleted`, "success");
    setSchemas((prev) => prev.filter((s) => s.id !== deleteTarget.id));
  };

  // ── Skeleton rows ──────────────────────────────────────────────────────────

  const skeletonRows = Array.from({ length: 5 }, (_, i) => (
    <tr key={`skel-${i}`} className={i % 2 === 0 ? "bg-surface-950/50" : ""}>
      {[1, 2, 3, 4, 5].map((col) => (
        <td key={col} className="px-4 py-3">
          <div className="h-4 rounded bg-surface-800 animate-pulse" style={{ width: col === 2 ? "140px" : "80px" }} />
        </td>
      ))}
    </tr>
  ));

  // ── Empty state ────────────────────────────────────────────────────────────

  const emptyRow = (
    <tr>
      <td colSpan={6}>
        <div className="flex flex-col items-center justify-center py-16 text-surface-500">
          <FileJson size={40} className="mb-3 opacity-40" />
          <p className="text-sm font-medium">No schemas found</p>
          <p className="text-xs mt-1">Create your first schema to define extraction structure</p>
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
          <h1 className="text-2xl font-bold tracking-tight">Extraction Schemas</h1>
          <p className="text-sm text-surface-400 mt-1">Configure entity types and extraction schemas</p>
        </div>
        <button onClick={() => setShowCreate(true)} className="btn-primary text-sm">
          <Plus size={16} />
          Create Schema
        </button>
      </div>

      {/* Table */}
      <div className="card-base overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-surface-800">
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Name</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Type</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Status</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Template</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Created</th>
                <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-surface-400">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-surface-800">
              {loading
                ? skeletonRows
                : schemas.length === 0
                  ? emptyRow
                  : schemas.map((schema, idx) => (
                      <tr
                        key={schema.id}
                        className={cn(
                          "transition-colors hover:bg-surface-800/50",
                          idx % 2 === 0 ? "bg-surface-950/50" : "",
                        )}
                      >
                        {/* Name */}
                        <td className="px-4 py-3">
                          <span className="font-medium text-white">{schema.name}</span>
                        </td>

                        {/* Type chip */}
                        <td className="px-4 py-3">
                          <span
                            className={cn(
                              "inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium capitalize",
                              schema.type === "structured"
                                ? "bg-brand-500/10 text-brand-300"
                                : "bg-accent-300/10 text-accent-300",
                            )}
                          >
                            {schema.type}
                          </span>
                        </td>

                        {/* Active status */}
                        <td className="px-4 py-3">
                          {schema.is_active ? (
                            <span className="inline-flex items-center rounded-full bg-success/10 text-success px-2.5 py-0.5 text-xs font-medium">
                              Active
                            </span>
                          ) : (
                            <span className="inline-flex items-center rounded-full bg-surface-700 text-surface-400 px-2.5 py-0.5 text-xs font-medium">
                              Inactive
                            </span>
                          )}
                        </td>

                        {/* Has prompt template? */}
                        <td className="px-4 py-3">
                          {schema.prompt_template ? (
                            <span className="text-xs text-surface-400">Yes</span>
                          ) : (
                            <span className="text-xs text-surface-600">—</span>
                          )}
                        </td>

                        {/* Created */}
                        <td className="px-4 py-3 text-surface-400 text-xs">
                          {formatDate(schema.created_at)}
                        </td>

                        {/* Actions */}
                        <td className="px-4 py-3 text-right">
                          <div className="flex items-center justify-end gap-1">
                            <button
                              onClick={() => setViewTarget(schema)}
                              className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-white"
                              title="View schema"
                            >
                              <Eye size={14} />
                            </button>
                            <button
                              onClick={() => setDeleteTarget(schema)}
                              className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-error"
                              title="Delete schema"
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
      </div>

      {/* ── Create Dialog ─────────────────────────────────────────────────────── */}
      {showCreate && (
        <CreateDialog
          onClose={() => setShowCreate(false)}
          onCreate={handleCreate}
        />
      )}

      {/* ── View Dialog ───────────────────────────────────────────────────────── */}
      {viewTarget && (
        <ViewDialog
          schema={viewTarget}
          onClose={() => setViewTarget(null)}
        />
      )}

      {/* ── Delete Dialog ─────────────────────────────────────────────────────── */}
      {deleteTarget && (
        <DeleteDialog
          schemaName={deleteTarget.name}
          onClose={() => setDeleteTarget(null)}
          onConfirm={handleDelete}
        />
      )}

      {/* ── Toast ─────────────────────────────────────────────────────────────── */}
      <Toast toast={toast} onDismiss={dismissToast} />
    </div>
  </RequireAuth>
  );
}
