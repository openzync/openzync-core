"use client";
import { RequireAuth } from "../../../require-auth";

import { useEffect, useState, useCallback } from "react";
import { Save, X, GitBranch } from "lucide-react";
import { toast } from "sonner";
import { get, patch, ApiError } from "@/lib/api-client";
import { ErrorState } from "@/components/shared/error-state";
import { Button } from "@/components/ui/button";

// ─── Types ─────────────────────────────────────────────────────────────────────

interface OrgConfigResponse {
  stored: Record<string, unknown>;
}

type GraphBackend = "postgres" | "graphiti" | "none";
type GraphSearchType = "hybrid" | "bm25" | "vector";

interface FormState {
  graph_backend: GraphBackend;
  graph_search_type: GraphSearchType;
  graph_max_traversal_depth: number;
  falkordb_url: string;
}

// ─── Constants ─────────────────────────────────────────────────────────────────

const FIELDS: (keyof FormState)[] = [
  "graph_backend",
  "graph_search_type",
  "graph_max_traversal_depth",
  "falkordb_url",
];

const GRAPH_BACKEND_OPTIONS: { value: GraphBackend; label: string }[] = [
  { value: "postgres", label: "PostgreSQL (pgvector)" },
  { value: "graphiti", label: "Graphiti (FalkorDB)" },
  { value: "none", label: "No graph backend" },
];

const SEARCH_TYPE_OPTIONS: { value: GraphSearchType; label: string }[] = [
  { value: "hybrid", label: "Hybrid (vector + keyword)" },
  { value: "bm25", label: "BM25 (keyword)" },
  { value: "vector", label: "Vector" },
];

// ─── Main Page ─────────────────────────────────────────────────────────────────

export default function GraphConfigPage() {
  const [form, setForm] = useState<FormState>({
    graph_backend: "postgres",
    graph_search_type: "hybrid",
    graph_max_traversal_depth: 3,
    falkordb_url: "",
  });
  const [initialForm, setInitialForm] = useState<FormState>({ ...form });
  const [stored, setStored] = useState<Record<string, unknown>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // ── Fetch config ──────────────────────────────────────────────────────────

  const fetchConfig = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await get<OrgConfigResponse>("/admin/org/config");
      const stored_: Record<string, unknown> = data.stored as Record<string, unknown>;
      const hasAnyStored = FIELDS.some((f) => stored_[f] != null);

      // If no stored values exist for this tab, pull onboarding defaults from API
      let defaults: Record<string, unknown> = {};
      if (!hasAnyStored) {
        try {
          defaults = await get<Record<string, unknown>>(
            "/admin/org/config/defaults",
          );
        } catch {
          // best-effort; fall through to inline fallbacks
        }
      }

      const val = (field: string, fallback: unknown) =>
        (stored_[field] as unknown) ?? (defaults[field] as unknown) ?? fallback;

      const current: FormState = {
        graph_backend: val("graph_backend", "postgres") as GraphBackend,
        graph_search_type: val("graph_search_type", "hybrid") as GraphSearchType,
        graph_max_traversal_depth: val("graph_max_traversal_depth", 3) as number,
        falkordb_url: val("falkordb_url", "") as string,
      };
      setForm(current);
      setInitialForm(current);
      setStored(data.stored ?? {});
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else if (err instanceof Error) {
        setError(err.message);
      } else {
        setError("Failed to load configuration");
      }
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
      await patch("/admin/org/config", { [field]: null });
      toast.success(`"${field}" reset to default`);
      await fetchConfig();
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : "Failed to reset field";
      toast.error(message);
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

      await patch("/admin/org/config", changed);
      toast.success("Graph configuration saved successfully");
      await fetchConfig();
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : "Failed to save configuration";
      setError(message);
      toast.error(message);
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
            {error && <ErrorState message={error} onRetry={fetchConfig} />}
            <div className={error ? "space-y-4 max-w-md mt-4" : "space-y-4 max-w-md"}>
              {/* graph_backend */}
              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1">
                  Graph Backend
                </label>
                <div className="flex gap-2 items-start">
                  <select
                    className="input-base flex-1"
                    value={form.graph_backend}
                    onChange={(e) => updateField("graph_backend", e.target.value as GraphBackend)}
                  >
                    {GRAPH_BACKEND_OPTIONS.map((opt) => (
                      <option key={opt.value} value={opt.value}>{opt.label}</option>
                    ))}
                  </select>
                  {isFieldSet("graph_backend") && (
                    <button
                      onClick={() => handleResetField("graph_backend")}
                      className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-brand-300 shrink-0 mt-0.5"
                      title="Reset to default"
                    >
                      <X size={14} />
                    </button>
                  )}
                </div>
              </div>

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

              {/* falkordb_url — conditionally shown */}
              {form.graph_backend === "graphiti" && (
                <div>
                  <label className="block text-sm font-medium text-surface-300 mb-1">
                    FalkorDB URL
                  </label>
                  <div className="flex gap-2 items-start">
                    <input
                      className="input-base flex-1"
                      type="url"
                      placeholder="falkordb://username:password@host:port"
                      value={form.falkordb_url}
                      onChange={(e) => updateField("falkordb_url", e.target.value)}
                    />
                    {isFieldSet("falkordb_url") && (
                      <button
                        onClick={() => handleResetField("falkordb_url")}
                        className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-brand-300 shrink-0 mt-0.5"
                        title="Reset to default"
                      >
                        <X size={14} />
                      </button>
                    )}
                  </div>
                  <p className="text-xs text-surface-500 mt-1">Required when using Graphiti backend</p>
                </div>
              )}

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
          <Button variant="primary" size="sm" icon={<Save size={14} />} loading={saving} disabled={saving || !hasChanged()} onClick={handleSave}>
            Save Changes
          </Button>
          {!hasChanged() && (
            <span className="text-xs text-surface-500">No changes to save</span>
          )}
          {hasChanged() && (
            <Button variant="secondary" size="sm" onClick={() => setForm({ ...initialForm })}>
              Discard Changes
            </Button>
          )}
        </div>
      )}
    </div>
  </RequireAuth>
  );
}
