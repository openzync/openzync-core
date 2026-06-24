"use client";
import { RequireAuth } from "../../require-auth";

import { useEffect, useState, useCallback } from "react";
import {
  Plus,
  FileText,
  X,
  Pencil,
  Trash2,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { toast } from "sonner";
import { get, put, ApiError } from "@/lib/api-client";
import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/empty-state";
import { ErrorState } from "@/components/shared/error-state";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { Button } from "@/components/ui/button";
import { TableSkeleton } from "@/components/shared/skeleton";

// ─── Types ─────────────────────────────────────────────────────────────────────

interface CustomInstruction {
  name: string;
  text: string;
}

// ─── Page ──────────────────────────────────────────────────────────────────────

export default function ExtractionInstructionsPage() {
  const [instructions, setInstructions] = useState<CustomInstruction[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Create / Edit dialog
  const [showDialog, setShowDialog] = useState(false);
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [formName, setFormName] = useState("");
  const [formText, setFormText] = useState("");
  const [saving, setSaving] = useState(false);

  // Delete dialog
  const [deleteTarget, setDeleteTarget] = useState<CustomInstruction | null>(null);
  const [deleting, setDeleting] = useState(false);

  // ── Fetch ──────────────────────────────────────────────────────────────────

  const fetchInstructions = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await get<{ data: CustomInstruction[] }>("/admin/org/custom-instructions");
      setInstructions(data.data ?? []);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load instructions");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchInstructions(); }, [fetchInstructions]);

  // ── Save (create or update) ─────────────────────────────────────────────────

  const openCreate = () => {
    setEditingIndex(null);
    setFormName("");
    setFormText("");
    setShowDialog(true);
  };

  const openEdit = (index: number) => {
    const inst = instructions[index];
    setEditingIndex(index);
    setFormName(inst.name);
    setFormText(inst.text);
    setShowDialog(true);
  };

  const handleSave = async () => {
    if (!formName.trim() || !formText.trim()) return;
    setSaving(true);

    // Optimistic update
    const updated = [...instructions];
    if (editingIndex !== null) {
      updated[editingIndex] = { name: formName.trim(), text: formText.trim() };
    } else {
      updated.push({ name: formName.trim(), text: formText.trim() });
    }

    try {
      await put("/admin/org/custom-instructions", { data: updated });
      setInstructions(updated);
      setShowDialog(false);
      toast.success(editingIndex !== null ? "Instruction updated" : "Instruction created");
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Failed to save instructions";
      setError(msg);
      toast.error(msg);
      fetchInstructions();
    } finally {
      setSaving(false);
    }
  };

  // ── Delete ─────────────────────────────────────────────────────────────────

  const handleDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);

    const updated = instructions.filter((i) => i.name !== deleteTarget.name);

    try {
      await put("/admin/org/custom-instructions", { data: updated });
      setInstructions(updated);
      setDeleteTarget(null);
      toast.success("Instruction deleted");
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Failed to delete instruction";
      setError(msg);
      toast.error(msg);
      fetchInstructions();
    } finally {
      setDeleting(false);
    }
  };

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
    <RequireAuth>
    <div className="space-y-6">
      <PageHeader
        title="Extraction Instructions"
        description="Custom instructions for extraction behavior"
        actions={
          <Button variant="primary" size="sm" icon={<Plus size={14} />} onClick={openCreate}>
            Add Instruction
          </Button>
        }
      />

      {/* Error */}
      {error && <ErrorState message={error} onRetry={fetchInstructions} />}

      {/* Table */}
      <div className="card-base overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-surface-800">
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Name</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Instruction</th>
                <th className="px-4 py-3 text-center text-xs font-medium uppercase tracking-wider text-surface-400 w-20">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-surface-800">
              {loading ? (
                <TableSkeleton rows={3} cols={3} colWidths={["w-32", "w-full", "w-16"]} />
              ) : instructions.length === 0 ? (
                <tr>
                  <td colSpan={3}>
                    <EmptyState
                      icon={FileText}
                      title="No instructions yet"
                      description="Add custom instructions to guide extraction behavior"
                      action={<Button variant="primary" size="sm" icon={<Plus size={14} />} onClick={openCreate}>Add Instruction</Button>}
                    />
                  </td>
                </tr>
              ) : (
                instructions.map((inst, idx) => (
                  <tr
                    key={inst.name}
                    className={cn("transition-colors hover:bg-surface-800/50", idx % 2 === 0 ? "bg-surface-950/50" : "")}
                  >
                    <td className="px-4 py-3">
                      <span className="text-surface-200 font-medium">{inst.name}</span>
                    </td>
                    <td className="px-4 py-3">
                      <span className="text-surface-400 text-xs block max-w-md truncate">
                        {inst.text.length > 80 ? inst.text.slice(0, 80) + "..." : inst.text}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-center">
                      <div className="flex items-center justify-center gap-1">
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => openEdit(idx)}
                          className="rounded-md text-surface-400 hover:text-white"
                          title="Edit instruction"
                        >
                          <Pencil size={14} />
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => setDeleteTarget(inst)}
                          className="rounded-md text-surface-400 hover:text-error"
                          title="Delete instruction"
                        >
                          <Trash2 size={14} />
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* ── Create / Edit Dialog ───────────────────────────────────────────────── */}
      {showDialog && (
        <>
          <div className="fixed inset-0 z-50 bg-black/50" onClick={() => setShowDialog(false)} />
          <div className="fixed left-1/2 top-1/2 z-50 w-full max-w-lg -translate-x-1/2 -translate-y-1/2 rounded-lg border border-surface-800 bg-surface-900 p-6 shadow-xl shadow-black/40 animate-slide-up">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-semibold text-text-primary">
                {editingIndex !== null ? "Edit Instruction" : "Add Instruction"}
              </h3>
              <button onClick={() => setShowDialog(false)} className="text-surface-400 hover:text-white">
                <X size={18} />
              </button>
            </div>

            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1.5">Name</label>
                <input
                  className="input-base"
                  placeholder="e.g. financial_data"
                  value={formName}
                  onChange={(e) => setFormName(e.target.value)}
                  autoFocus
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1.5">Instruction Text</label>
                <textarea
                  className="input-base min-h-[120px] pt-2 text-sm"
                  placeholder="Describe what to extract and how..."
                  value={formText}
                  onChange={(e) => setFormText(e.target.value)}
                />
              </div>
            </div>

            <div className="flex justify-end gap-3 mt-6">
              <Button variant="secondary" size="sm" onClick={() => setShowDialog(false)}>Cancel</Button>
              <Button variant="primary" size="sm" onClick={handleSave} loading={saving} disabled={!formName.trim() || !formText.trim()}>
                {editingIndex !== null ? "Save Changes" : "Add"}
              </Button>
            </div>
          </div>
        </>
      )}

      {/* ── Delete Confirm Dialog ──────────────────────────────────────────────── */}
      <ConfirmDialog
        open={!!deleteTarget}
        title="Delete Instruction"
        message={`Are you sure you want to delete "${deleteTarget?.name}"?`}
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
