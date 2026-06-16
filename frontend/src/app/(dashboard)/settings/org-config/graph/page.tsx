"use client";
import { RequireAuth } from "../../../require-auth";

import { useEffect, useState, useCallback } from "react";
import {
  Save,
  CheckCircle,
  AlertCircle,
  X,
  GitBranch,
} from "lucide-react";
import { cn } from "@/lib/utils";

// ─── Types ─────────────────────────────────────────────────────────────────────

interface OrgConfigResponse {
  stored: Record<string, unknown>;
}

type GraphSearchType = "hybrid" | "bm25" | "vector";

interface FormState {
  graph_search_type: GraphSearchType;
  graph_max_traversal_depth: number;
}

interface ToastState {
  visible: boolean;
  message: string;
  type: "success" | "error";
}

// ─── Constants ─────────────────────────────────────────────────────────────────

const API_BASE = "http://localhost:8000";
const TOAST_DURATION = 4000;

const FIELDS: (keyof FormState)[] = [
  "graph_search_type",
  "graph_max_traversal_depth",
];

const SEARCH_TYPE_OPTIONS: { value: GraphSearchType; label: string }[] = [
  { value: "hybrid", label: "Hybrid (vector + keyword)" },
  { value: "bm25", label: "BM25 (keyword)" },
  { value: "vector", label: "Vector" },
];

// ─── Helpers ───────────────────────────────────────────────────────────────────

function authHeaders(): Record<string, string> {
  const token = sessionStorage.getItem("mg_access_token");
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  return headers;
}

// ─── Toast ─────────────────────────────────────────────────────────────────────

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

// ─── Defaults ──────────────────────────────────────────────────────────────────

const DEFAULT_FORM: FormState = {
  graph_search_type: "hybrid",
  graph_max_traversal_depth: 3,
};

// ─── Main Page ─────────────────────────────────────────────────────────────────

export default function GraphConfigPage() {
  const [form, setForm] = useState<FormState>({ ...DEFAULT_FORM });
  const [initialForm, setInitialForm] = useState<FormState>({ ...DEFAULT_FORM });
  const [stored, setStored] = useState<Record<string, unknown>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [toast, setToast] = useState<ToastState>({ visible: false, message: "", type: "success" });

  const showToast = useCallback((message: string, type: "success" | "error") => {
    setToast({ visible: true, message, type });
  }, []);

  const dismissToast = useCallback(() => {
    setToast((prev) => ({ ...prev, visible: false }));
  }, []);

  // ── Fetch config ──────────────────────────────────────────────────────────

  const fetchConfig = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/admin/org/config`, { headers: authHeaders() });
      if (!res.ok) throw new Error("Failed to load configuration");
      const data: OrgConfigResponse = await res.json();

      const stored = data.stored as Record<string, unknown>;
      setForm({
        graph_search_type: (stored.graph_search_type as GraphSearchType) ?? "hybrid",
        graph_max_traversal_depth: (stored.graph_max_traversal_depth as number) ?? 3,
      });
      setInitialForm({
        graph_search_type: (stored.graph_search_type as GraphSearchType) ?? "hybrid",
        graph_max_traversal_depth: (stored.graph_max_traversal_depth as number) ?? 3,
      });
      setStored(data.stored ?? {});
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load configuration");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchConfig();
  }, [fetchConfig]);

  // ── Field helpers ─────────────────────────────────────────────────────────

  function isFieldSet(field: string): boolean {
    return field in stored;
  }

  function hasChanged(): boolean {
    return FIELDS.some((f) => form[f] !== initialForm[f]);
  }

  function updateField<K extends keyof FormState>(field: K, value: FormState[K]) {
    setForm((prev) => ({ ...prev, [field]: value }));
  }

  // ── Reset field to default ────────────────────────────────────────────────

  async function handleResetField(field: keyof FormState) {
    try {
      const res = await fetch(`${API_BASE}/admin/org/config`, {
        method: "PATCH",
        headers: authHeaders(),
        body: JSON.stringify({ [field]: null }),
      });
      if (!res.ok) throw new Error("Failed to reset field");
      showToast(`"${field}" reset to default`, "success");
      await fetchConfig();
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Failed to reset field", "error");
    }
  }

  // ── Save ──────────────────────────────────────────────────────────────────

  async function handleSave() {
    if (!hasChanged()) return;
    setSaving(true);
    setError(null);

    try {
      const changed: Record<string, unknown> = {};
      for (const field of FIELDS) {
        if (form[field] !== initialForm[field]) {
          changed[field] = form[field];
        }
      }

      const res = await fetch(`${API_BASE}/admin/org/config`, {
        method: "PATCH",
        headers: authHeaders(),
        body: JSON.stringify(changed),
      });

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail ?? "Failed to save configuration");
      }

      showToast("Graph configuration saved successfully", "success");
      await fetchConfig();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save configuration");
      showToast(err instanceof Error ? err.message : "Failed to save configuration", "error");
    } finally {
      setSaving(false);
    }
  }

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
    <RequireAuth>
    <div className="space-y-6">
      {/* ── Graph Configuration Card ──────────────────────────────────────────── */}
      <div className="card-base p-6">
        <div className="flex items-center gap-3 mb-6">
          <div className="flex h-10 w-10 items-center justify-center rounded-full bg-brand-500/10">
            <GitBranch size={20} className="text-brand-300" />
          </div>
          <div>
            <h2 className="text-base font-semibold">Graph Settings</h2>
            <p className="text-xs text-surface-400">Knowledge graph search and traversal</p>
          </div>
        </div>

        {loading ? (
          <div className="space-y-4">
            <div className="h-9 rounded bg-surface-800 animate-pulse w-full" />
            <div className="h-9 rounded bg-surface-800 animate-pulse w-full" />
            <div className="h-9 rounded bg-surface-800 animate-pulse w-48" />
          </div>
        ) : (
          <>
            {error && (
              <div className="rounded-md bg-error/10 border border-error/30 px-3 py-2 mb-4 text-sm text-error flex items-center gap-2">
                <AlertCircle size={14} />
                {error}
              </div>
            )}
            <div className="space-y-4 max-w-md">
              {/* graph_search_type */}
              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1">
                  Search Type
                  
                </label>
                <div className="flex gap-2 items-start">
                  <select
                    className="input-base flex-1"
                    value={form.graph_search_type}
                    onChange={(e) => updateField("graph_search_type", e.target.value as GraphSearchType)}
                  >
                    {SEARCH_TYPE_OPTIONS.map((opt) => (
                      <option key={opt.value} value={opt.value}>{opt.label}</option>
                    ))}
                  </select>
                  {isFieldSet("graph_search_type") && (
                    <button
                      onClick={() => handleResetField("graph_search_type")}
                      className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-brand-300 shrink-0 mt-0.5"
                      title="Reset to default"
                    >
                      <X size={14} />
                    </button>
                  )}
                </div>
              </div>

              {/* graph_max_traversal_depth */}
              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1">
                  Max Traversal Depth
                  
                </label>
                <div className="flex gap-2 items-start">
                  <input
                    className="input-base flex-1"
                    type="number"
                    min="1"
                    max="10"
                    value={form.graph_max_traversal_depth}
                    onChange={(e) => updateField("graph_max_traversal_depth", parseInt(e.target.value) || 1)}
                  />
                  {isFieldSet("graph_max_traversal_depth") && (
                    <button
                      onClick={() => handleResetField("graph_max_traversal_depth")}
                      className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-brand-300 shrink-0 mt-0.5"
                      title="Reset to default"
                    >
                      <X size={14} />
                    </button>
                  )}
                </div>
                <p className="text-xs text-surface-500 mt-1">How many hops the graph traversal will follow (1&ndash;10)</p>
              </div>
            </div>
          </>
        )}
      </div>

      {/* ── Save Button ───────────────────────────────────────────────────────── */}
      {!loading && (
        <div className="flex items-center gap-3">
          <button
            onClick={handleSave}
            disabled={saving || !hasChanged()}
            className="btn-primary text-sm"
          >
            <Save size={14} />
            {saving ? "Saving..." : "Save Changes"}
          </button>
          {!hasChanged() && (
            <span className="text-xs text-surface-500">No changes to save</span>
          )}
          {hasChanged() && (
            <button
              onClick={() => setForm({ ...initialForm })}
              className="btn-secondary text-sm"
            >
              Discard Changes
            </button>
          )}
        </div>
      )}

      {/* ── Toast ─────────────────────────────────────────────────────────────── */}
      <Toast toast={toast} onDismiss={dismissToast} />
    </div>
  </RequireAuth>
  );
}
