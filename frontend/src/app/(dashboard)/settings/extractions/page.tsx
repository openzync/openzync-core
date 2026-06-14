"use client";

import { useEffect, useState, useCallback } from "react";
import { Database, Eye, X, CheckCircle, AlertCircle } from "lucide-react";
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

        <div className="mb-4">
          <span className="text-xs font-medium text-surface-400 block mb-1.5">JSON Schema</span>
          <div className="bg-surface-950 border border-surface-700 font-mono text-xs p-4 rounded overflow-x-auto max-h-64 overflow-y-auto">
            <pre className="text-surface-200 whitespace-pre">{formattedJson}</pre>
          </div>
        </div>

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

// ─── Main Page ─────────────────────────────────────────────────────────────────

export default function ExtractionsPage() {
  const [schemas, setSchemas] = useState<Schema[]>([]);
  const [loading, setLoading] = useState(true);
  const [viewTarget, setViewTarget] = useState<Schema | null>(null);

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
      const res = await fetch(`${API_BASE}/v1/admin/schemas?type=structured`, {
        headers: authHeaders(),
      });
      if (!res.ok) throw new Error("Failed to load extraction schemas");
      const data: SchemasResponse = await res.json();
      setSchemas(data.data ?? []);
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Failed to load extractions", "error");
    } finally {
      setLoading(false);
    }
  }, [showToast]);

  useEffect(() => {
    fetchSchemas();
  }, [fetchSchemas]);

  // ── Skeleton rows ──────────────────────────────────────────────────────────

  const skeletonRows = Array.from({ length: 4 }, (_, i) => (
    <tr key={`skel-${i}`} className={i % 2 === 0 ? "bg-surface-950/50" : ""}>
      {[1, 2, 3, 4].map((col) => (
        <td key={col} className="px-4 py-3">
          <div className="h-4 rounded bg-surface-800 animate-pulse" style={{ width: col === 1 ? "140px" : "80px" }} />
        </td>
      ))}
    </tr>
  ));

  // ── Empty state ────────────────────────────────────────────────────────────

  const emptyRow = (
    <tr>
      <td colSpan={5}>
        <div className="flex flex-col items-center justify-center py-16 text-surface-500">
          <Database size={40} className="mb-3 opacity-40" />
          <p className="text-sm font-medium">No extraction schemas found</p>
          <p className="text-xs mt-1">Configure extraction schemas to appear here</p>
        </div>
      </td>
    </tr>
  );

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Structured Extractions</h1>
        <p className="text-sm text-surface-400 mt-1">Structured data extraction schemas and results</p>
      </div>

      {/* Table */}
      <div className="card-base overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-surface-800">
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Name</th>
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
                        <td className="px-4 py-3">
                          <span className="font-medium text-white">{schema.name}</span>
                        </td>
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
                        <td className="px-4 py-3">
                          {schema.prompt_template ? (
                            <span className="text-xs text-surface-400">Yes</span>
                          ) : (
                            <span className="text-xs text-surface-600">—</span>
                          )}
                        </td>
                        <td className="px-4 py-3 text-surface-400 text-xs">
                          {formatDate(schema.created_at)}
                        </td>
                        <td className="px-4 py-3 text-right">
                          <button
                            onClick={() => setViewTarget(schema)}
                            className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-white"
                            title="View schema"
                          >
                            <Eye size={14} />
                          </button>
                        </td>
                      </tr>
                    ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* ── View Dialog ───────────────────────────────────────────────────────── */}
      {viewTarget && (
        <ViewDialog
          schema={viewTarget}
          onClose={() => setViewTarget(null)}
        />
      )}

      {/* ── Toast ─────────────────────────────────────────────────────────────── */}
      <Toast toast={toast} onDismiss={dismissToast} />
    </div>
  );
}
