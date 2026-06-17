"use client";
import { RequireAuth } from "../../require-auth";

import { useEffect, useState, useCallback } from "react";
import {
  Plus,
  Eye,
  Trash2,
  X,
  FileJson,
  AlertCircle,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { toast } from "sonner";
import { get, post, del, ApiError } from "@/lib/api-client";
import { formatDate } from "@/lib/utils";
import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/empty-state";
import { ErrorState } from "@/components/shared/error-state";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { TableSkeleton } from "@/components/shared/skeleton";

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

// ─── Page ──────────────────────────────────────────────────────────────────────

export default function SchemasPage() {
  const [schemas, setSchemas] = useState<Schema[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Create dialog
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState("");
  const [newType, setNewType] = useState<"structured" | "classification">("structured");
  const [newSchema, setNewSchema] = useState("{\n  \n}");
  const [newPrompt, setNewPrompt] = useState("");
  const [creating, setCreating] = useState(false);
  const [schemaError, setSchemaError] = useState<string | null>(null);

  // View dialog
  const [viewTarget, setViewTarget] = useState<Schema | null>(null);

  // Delete dialog
  const [deleteTarget, setDeleteTarget] = useState<Schema | null>(null);
  const [deleting, setDeleting] = useState(false);

  // ── Fetch ──────────────────────────────────────────────────────────────────

  const fetchSchemas = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await get<{ data: Schema[] }>("/v1/admin/schemas");
      setSchemas(data.data ?? []);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load schemas");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchSchemas(); }, [fetchSchemas]);

  // ── Create ─────────────────────────────────────────────────────────────────

  const handleCreate = async () => {
    if (!newName.trim()) return;
    setSchemaError(null);

    // Validate JSON schema
    try {
      JSON.parse(newSchema);
    } catch {
      setSchemaError("Invalid JSON schema");
      return;
    }

    setCreating(true);
    try {
      const payload: Record<string, unknown> = {
        name: newName.trim(),
        type: newType,
        json_schema: JSON.parse(newSchema),
      };
      if (newPrompt.trim()) payload.prompt_template = newPrompt.trim();

      await post("/v1/admin/schemas", payload);
      setShowCreate(false);
      resetCreateForm();
      toast.success("Schema created");
      fetchSchemas();
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Failed to create schema";
      setError(msg);
      toast.error(msg);
    } finally {
      setCreating(false);
    }
  };

  const resetCreateForm = () => {
    setNewName("");
    setNewType("structured");
    setNewSchema("{\n  \n}");
    setNewPrompt("");
    setSchemaError(null);
  };

  // ── Delete ─────────────────────────────────────────────────────────────────

  const handleDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await del(`/v1/admin/schemas/${deleteTarget.id}`);
      setDeleteTarget(null);
      toast.success("Schema deleted");
      fetchSchemas();
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Failed to delete schema";
      setError(msg);
      toast.error(msg);
    } finally {
      setDeleting(false);
    }
  };

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
    <RequireAuth>
    <div className="space-y-6">
      <PageHeader
        title="Extraction Schemas"
        description="Define schemas for structured extractions and classifications"
        actions={
          <Button variant="primary" size="sm" icon={<Plus size={14} />} onClick={() => setShowCreate(true)}>
            Create Schema
          </Button>
        }
      />

      {/* Error */}
      {error && <ErrorState message={error} onRetry={fetchSchemas} />}

      {/* Table */}
      <div className="card-base overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-surface-800">
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Name</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Type</th>
                <th className="px-4 py-3 text-center text-xs font-medium uppercase tracking-wider text-surface-400">Status</th>
                <th className="px-4 py-3 text-center text-xs font-medium uppercase tracking-wider text-surface-400">Template</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Created</th>
                <th className="px-4 py-3 text-center text-xs font-medium uppercase tracking-wider text-surface-400 w-20">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-surface-800">
              {loading ? (
                <TableSkeleton rows={4} cols={6} colWidths={["w-36", "w-24", "w-16", "w-16", "w-24", "w-16"]} />
              ) : schemas.length === 0 ? (
                <tr>
                  <td colSpan={6}>
                    <EmptyState
                      icon={FileJson}
                      title="No schemas yet"
                      description="Create your first extraction schema"
                      action={<Button variant="primary" size="sm" icon={<Plus size={14} />} onClick={() => setShowCreate(true)}>Create Schema</Button>}
                    />
                  </td>
                </tr>
              ) : (
                schemas.map((schema, idx) => (
                  <tr
                    key={schema.id}
                    className={cn("transition-colors hover:bg-surface-800/50", idx % 2 === 0 ? "bg-surface-950/50" : "")}
                  >
                    <td className="px-4 py-3">
                      <span className="text-surface-200 font-medium">{schema.name}</span>
                    </td>
                    <td className="px-4 py-3">
                      <Badge variant={schema.type === "classification" ? "info" : "brand"} size="sm">
                        {schema.type}
                      </Badge>
                    </td>
                    <td className="px-4 py-3 text-center">
                      <Badge variant={schema.is_active ? "success" : "default"} size="sm">
                        {schema.is_active ? "Active" : "Inactive"}
                      </Badge>
                    </td>
                    <td className="px-4 py-3 text-center">
                      <span className="text-xs text-surface-400">
                        {schema.prompt_template ? "Yes" : "—"}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      <span className="text-surface-400 text-xs">{formatDate(schema.created_at)}</span>
                    </td>
                    <td className="px-4 py-3 text-center">
                      <div className="flex items-center justify-center gap-1">
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
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* ── Create Dialog ──────────────────────────────────────────────────────── */}
      {showCreate && (
        <>
          <div className="fixed inset-0 z-50 bg-black/50" onClick={() => { setShowCreate(false); resetCreateForm(); }} />
          <div className="fixed left-1/2 top-1/2 z-50 w-full max-w-lg -translate-x-1/2 -translate-y-1/2 rounded-lg border border-surface-800 bg-surface-900 p-6 shadow-xl shadow-black/40 animate-slide-up max-h-[85vh] overflow-y-auto">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-semibold text-[#F2F2F2]">Create Schema</h3>
              <button onClick={() => { setShowCreate(false); resetCreateForm(); }} className="text-surface-400 hover:text-white">
                <X size={18} />
              </button>
            </div>

            <div className="space-y-4">
              {/* Name */}
              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1.5">Name</label>
                <input
                  className="input-base"
                  placeholder="e.g. invoice_data"
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  autoFocus
                />
              </div>

              {/* Type */}
              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1.5">Type</label>
                <select
                  className="input-base"
                  value={newType}
                  onChange={(e) => setNewType(e.target.value as "structured" | "classification")}
                >
                  <option value="structured">Structured</option>
                  <option value="classification">Classification</option>
                </select>
              </div>

              {/* JSON Schema */}
              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1.5">JSON Schema</label>
                <textarea
                  className="input-base min-h-[120px] pt-2 font-mono text-xs"
                  placeholder='{"type": "object", "properties": {...}}'
                  value={newSchema}
                  onChange={(e) => setNewSchema(e.target.value)}
                />
                {schemaError && (
                  <p className="text-xs text-error mt-1 flex items-center gap-1">
                    <AlertCircle size={10} />
                    {schemaError}
                  </p>
                )}
              </div>

              {/* Prompt Template (optional) */}
              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1.5">
                  Prompt Template <span className="text-surface-500 font-normal">(optional)</span>
                </label>
                <textarea
                  className="input-base min-h-[80px] pt-2 font-mono text-xs"
                  placeholder="Extract the following fields from the text..."
                  value={newPrompt}
                  onChange={(e) => setNewPrompt(e.target.value)}
                />
              </div>
            </div>

            <div className="flex justify-end gap-3 mt-6">
              <Button variant="secondary" size="sm" onClick={() => { setShowCreate(false); resetCreateForm(); }}>Cancel</Button>
              <Button variant="primary" size="sm" onClick={handleCreate} loading={creating} disabled={!newName.trim()}>
                Create
              </Button>
            </div>
          </div>
        </>
      )}

      {/* ── View Dialog ────────────────────────────────────────────────────────── */}
      {viewTarget && (
        <>
          <div className="fixed inset-0 z-50 bg-black/50" onClick={() => setViewTarget(null)} />
          <div className="fixed left-1/2 top-1/2 z-50 w-full max-w-lg -translate-x-1/2 -translate-y-1/2 rounded-lg border border-surface-800 bg-surface-900 p-6 shadow-xl shadow-black/40 animate-slide-up max-h-[85vh] overflow-y-auto">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-semibold text-[#F2F2F2]">{viewTarget.name}</h3>
              <button onClick={() => setViewTarget(null)} className="text-surface-400 hover:text-white">
                <X size={18} />
              </button>
            </div>

            <div className="space-y-3 text-sm">
              <div className="flex justify-between">
                <span className="text-surface-400">Type</span>
                <Badge variant={viewTarget.type === "classification" ? "info" : "brand"} size="sm">{viewTarget.type}</Badge>
              </div>
              <div className="flex justify-between">
                <span className="text-surface-400">Status</span>
                <Badge variant={viewTarget.is_active ? "success" : "default"} size="sm">
                  {viewTarget.is_active ? "Active" : "Inactive"}
                </Badge>
              </div>
              <div className="flex justify-between">
                <span className="text-surface-400">Created</span>
                <span className="text-surface-200">{formatDate(viewTarget.created_at)}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-surface-400">ID</span>
                <code className="text-xs text-surface-400 font-mono">{viewTarget.id.slice(0, 12)}...</code>
              </div>

              <div>
                <span className="text-surface-400 block mb-1">JSON Schema</span>
                <pre className="bg-surface-950 rounded-lg p-3 text-xs font-mono text-surface-300 overflow-x-auto max-h-40">
                  {JSON.stringify(viewTarget.json_schema, null, 2)}
                </pre>
              </div>

              {viewTarget.prompt_template && (
                <div>
                  <span className="text-surface-400 block mb-1">Prompt Template</span>
                  <pre className="bg-surface-950 rounded-lg p-3 text-xs font-mono text-surface-300 overflow-x-auto max-h-32 whitespace-pre-wrap">
                    {viewTarget.prompt_template}
                  </pre>
                </div>
              )}
            </div>

            <div className="flex justify-end mt-6">
              <Button variant="secondary" size="sm" onClick={() => setViewTarget(null)}>Close</Button>
            </div>
          </div>
        </>
      )}

      {/* ── Delete Confirm Dialog ──────────────────────────────────────────────── */}
      <ConfirmDialog
        open={!!deleteTarget}
        title="Delete Schema"
        message={`Are you sure you want to delete "${deleteTarget?.name}"? This action cannot be undone.`}
        confirmLabel="Delete"
        variant="danger"
        loading={deleting}
        onConfirm={handleDelete}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  </RequireAuth>
  );
}
