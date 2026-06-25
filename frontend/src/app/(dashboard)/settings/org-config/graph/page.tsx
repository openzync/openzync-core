"use client";

import { useEffect, useState, useCallback } from "react";
import { Save, X, GitBranch } from "lucide-react";
import { toast } from "sonner";
import { get, patch, ApiError } from "@/lib/api-client";
import { ErrorState } from "@/components/shared/error-state";
import { Button } from "@/components/ui/button";
import { SecretInput } from "@/components/ui/secret-input";

// ─── Types ─────────────────────────────────────────────────────────────────────

interface OrgConfigResponse {
  stored: Record<string, unknown>;
}

type GraphBackend = "postgres" | "surrealdb" | "none";
type GraphSearchType = "hybrid" | "bm25" | "vector";

interface FormState {
  graph_backend: GraphBackend;
  graph_search_type: GraphSearchType;
  graph_max_traversal_depth: number;
  surrealdb_url: string;
  surrealdb_user: string;
  surrealdb_pass: string;
  surrealdb_namespace: string;
  surrealdb_database: string;
}

// ─── Constants ─────────────────────────────────────────────────────────────────

const FIELDS: (keyof FormState)[] = [
  "graph_backend",
  "graph_search_type",
  "graph_max_traversal_depth",
  "surrealdb_url",
  "surrealdb_user",
  "surrealdb_pass",
  "surrealdb_namespace",
  "surrealdb_database",
];

const GRAPH_BACKEND_OPTIONS: { value: GraphBackend; label: string }[] = [
  { value: "postgres", label: "PostgreSQL (pgvector)" },
  { value: "surrealdb", label: "SurrealDB" },
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
    surrealdb_url: "",
    surrealdb_user: "",
    surrealdb_pass: "",
    surrealdb_namespace: "",
    surrealdb_database: "",
  });
  const [initialForm, setInitialForm] = useState<FormState>({ ...form });
  const [stored, setStored] = useState<Record<string, unknown>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showSurrealDbPass, setShowSurrealDbPass] = useState(false);

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
        surrealdb_url: val("surrealdb_url", "") as string,
        surrealdb_user: val("surrealdb_user", "") as string,
        surrealdb_pass: val("surrealdb_pass", "") as string,
        surrealdb_namespace: val("surrealdb_namespace", "") as string,
        surrealdb_database: val("surrealdb_database", "") as string,
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
    // eslint-disable-next-line react-hooks/set-state-in-effect
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
                    <Button
                      onClick={() => handleResetField("graph_backend")}
                      variant="ghost" size="sm" className="rounded-md text-surface-400 hover:text-brand-300 shrink-0 mt-0.5"
                      title="Reset to default"
                    >
                      <X size={14} />
                    </Button>
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
                    <Button
                      onClick={() => handleResetField("graph_search_type")}
                      variant="ghost" size="sm" className="rounded-md text-surface-400 hover:text-brand-300 shrink-0 mt-0.5"
                      title="Reset to default"
                    >
                      <X size={14} />
                    </Button>
                  )}
                </div>
              </div>

              {/* SurrealDB connection fields — conditionally shown */}
              {form.graph_backend === "surrealdb" && (
                <div className="space-y-4">
                  <div>
                    <label className="block text-sm font-medium text-surface-300 mb-1">
                      SurrealDB URL
                    </label>
                    <div className="flex gap-2 items-start">
                      <input
                        className="input-base flex-1"
                        type="url"
                        placeholder="ws://surrealdb:8000/rpc"
                        value={form.surrealdb_url}
                        onChange={(e) => updateField("surrealdb_url", e.target.value)}
                      />
                      {isFieldSet("surrealdb_url") && (
                        <Button
                          onClick={() => handleResetField("surrealdb_url")}
                          variant="ghost" size="sm" className="rounded-md text-surface-400 hover:text-brand-300 shrink-0 mt-0.5"
                          title="Reset to default"
                        >
                          <X size={14} />
                        </Button>
                      )}
                    </div>
                    <p className="text-xs text-surface-500 mt-1">Required when using SurrealDB backend</p>
                  </div>

                  {/* surrealdb_user */}
                  <div>
                    <label className="block text-sm font-medium text-surface-300 mb-1">
                      SurrealDB Username
                    </label>
                    <div className="flex gap-2 items-start">
                      <input
                        className="input-base flex-1"
                        type="text"
                        placeholder="SurrealDB username"
                        value={form.surrealdb_user}
                        onChange={(e) => updateField("surrealdb_user", e.target.value)}
                      />
                      {isFieldSet("surrealdb_user") && (
                        <Button
                          onClick={() => handleResetField("surrealdb_user")}
                          variant="ghost" size="sm" className="rounded-md text-surface-400 hover:text-brand-300 shrink-0 mt-0.5"
                          title="Reset to default"
                        >
                          <X size={14} />
                        </Button>
                      )}
                    </div>
                  </div>

                  {/* surrealdb_pass */}
                  <SecretInput
                    label="SurrealDB Password"
                    value={form.surrealdb_pass}
                    onChange={(v) => updateField("surrealdb_pass", v)}
                    placeholder="SurrealDB password"
                    visible={showSurrealDbPass}
                    onToggleVisibility={() => setShowSurrealDbPass((prev) => !prev)}
                  />

                  {/* surrealdb_namespace */}
                  <div>
                    <label className="block text-sm font-medium text-surface-300 mb-1">
                      SurrealDB Namespace
                    </label>
                    <div className="flex gap-2 items-start">
                      <input
                        className="input-base flex-1"
                        type="text"
                        placeholder="SurrealDB namespace"
                        value={form.surrealdb_namespace}
                        onChange={(e) => updateField("surrealdb_namespace", e.target.value)}
                      />
                      {isFieldSet("surrealdb_namespace") && (
                        <Button
                          onClick={() => handleResetField("surrealdb_namespace")}
                          variant="ghost" size="sm" className="rounded-md text-surface-400 hover:text-brand-300 shrink-0 mt-0.5"
                          title="Reset to default"
                        >
                          <X size={14} />
                        </Button>
                      )}
                    </div>
                  </div>

                  {/* surrealdb_database */}
                  <div>
                    <label className="block text-sm font-medium text-surface-300 mb-1">
                      SurrealDB Database
                    </label>
                    <div className="flex gap-2 items-start">
                      <input
                        className="input-base flex-1"
                        type="text"
                        placeholder="SurrealDB database"
                        value={form.surrealdb_database}
                        onChange={(e) => updateField("surrealdb_database", e.target.value)}
                      />
                      {isFieldSet("surrealdb_database") && (
                        <Button
                          onClick={() => handleResetField("surrealdb_database")}
                          variant="ghost" size="sm" className="rounded-md text-surface-400 hover:text-brand-300 shrink-0 mt-0.5"
                          title="Reset to default"
                        >
                          <X size={14} />
                        </Button>
                      )}
                    </div>
                  </div>
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
                    <Button
                      onClick={() => handleResetField("graph_max_traversal_depth")}
                      variant="ghost" size="sm" className="rounded-md text-surface-400 hover:text-brand-300 shrink-0 mt-0.5"
                      title="Reset to default"
                    >
                      <X size={14} />
                    </Button>
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
  );
}
