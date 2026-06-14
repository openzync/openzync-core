"use client";
import { RequireAuth } from "../../require-auth";

import { useEffect, useState, useCallback } from "react";
import {
  Plus,
  FileText,
  X,
  Pencil,
  Trash2,
  CheckCircle,
  AlertCircle,
} from "lucide-react";
import { cn } from "@/lib/utils";

// ─── Types ─────────────────────────────────────────────────────────────────────

interface CustomInstruction {
  name: string;
  text: string;
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
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (typeof window !== "undefined") {
    const token = sessionStorage.getItem("mg_access_token");
    if (token) headers["Authorization"] = `Bearer ${token}`;
  }
  return headers;
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
  onCreate: (name: string, text: string) => Promise<void>;
}

function CreateDialog({ onClose, onCreate }: CreateDialogProps) {
  const [name, setName] = useState("");
  const [instructionText, setInstructionText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmedName = name.trim();
    const trimmedText = instructionText.trim();
    if (!trimmedName) { setError("Instruction name is required"); return; }
    if (!trimmedText) { setError("Instruction text is required"); return; }

    setCreating(true);
    setError(null);
    try {
      await onCreate(trimmedName, trimmedText);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create instruction");
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 animate-fade-in" onClick={onClose}>
      <div
        className="card-base w-full max-w-lg p-6 shadow-xl shadow-black/40 animate-slide-up"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">Add Extraction Instruction</h2>
          <button onClick={onClose} className="btn-ghost p-1 rounded-md text-surface-400 hover:text-white">
            <X size={18} />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          {/* Name */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">
              Instruction Name <span className="text-error">*</span>
            </label>
            <input
              className="input-base"
              placeholder="e.g. Medical Terminology"
              value={name}
              onChange={(e) => { setName(e.target.value); if (error) setError(null); }}
              autoFocus
              disabled={creating}
              maxLength={255}
            />
            <p className="text-xs text-surface-500 mt-1">A short label for this instruction (max 255 characters).</p>
          </div>

          {/* Text */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">
              Instruction Text <span className="text-error">*</span>
            </label>
            <textarea
              className="input-base min-h-[120px] resize-y"
              placeholder="Define domain-specific terminology. Example: &apos;This system operates in the healthcare domain. Common terms include: diagnosis, prognosis, contraindication...&apos;"
              value={instructionText}
              onChange={(e) => { setInstructionText(e.target.value); if (error) setError(null); }}
              disabled={creating}
              rows={5}
            />
            <p className="text-xs text-surface-500 mt-1">
              Domain-specific guidance for the extraction pipeline.
            </p>
          </div>

          {error && (
            <div className="rounded-md bg-error/10 border border-error/30 px-3 py-2 text-sm text-error flex items-center gap-2">
              <AlertCircle size={14} />
              {error}
            </div>
          )}

          <div className="flex justify-end gap-3 pt-2 border-t border-surface-800">
            <button type="button" onClick={onClose} className="btn-secondary text-sm" disabled={creating}>
              Cancel
            </button>
            <button type="submit" disabled={creating} className="btn-primary text-sm min-w-[140px] justify-center">
              {creating ? (
                <span className="flex items-center gap-2">
                  <Spinner /> Adding...
                </span>
              ) : (
                "Add Instruction"
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ─── Edit Dialog ───────────────────────────────────────────────────────────────

interface EditDialogProps {
  instruction: CustomInstruction;
  onClose: () => void;
  onSave: (name: string, text: string) => Promise<void>;
}

function EditDialog({ instruction, onClose, onSave }: EditDialogProps) {
  const [name, setName] = useState(instruction.name);
  const [instructionText, setInstructionText] = useState(instruction.text);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmedName = name.trim();
    const trimmedText = instructionText.trim();
    if (!trimmedName) { setError("Instruction name is required"); return; }
    if (!trimmedText) { setError("Instruction text is required"); return; }

    setSaving(true);
    setError(null);
    try {
      await onSave(trimmedName, trimmedText);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save instruction");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 animate-fade-in" onClick={onClose}>
      <div
        className="card-base w-full max-w-lg p-6 shadow-xl shadow-black/40 animate-slide-up"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">Edit Extraction Instruction</h2>
          <button onClick={onClose} className="btn-ghost p-1 rounded-md text-surface-400 hover:text-white">
            <X size={18} />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          {/* Name */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">
              Instruction Name <span className="text-error">*</span>
            </label>
            <input
              className="input-base"
              placeholder="e.g. Medical Terminology"
              value={name}
              onChange={(e) => { setName(e.target.value); if (error) setError(null); }}
              autoFocus
              disabled={saving}
              maxLength={255}
            />
            <p className="text-xs text-surface-500 mt-1">A short label for this instruction (max 255 characters).</p>
          </div>

          {/* Text */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">
              Instruction Text <span className="text-error">*</span>
            </label>
            <textarea
              className="input-base min-h-[120px] resize-y"
              placeholder="Define domain-specific terminology. Example: &apos;This system operates in the healthcare domain. Common terms include: diagnosis, prognosis, contraindication...&apos;"
              value={instructionText}
              onChange={(e) => { setInstructionText(e.target.value); if (error) setError(null); }}
              disabled={saving}
              rows={5}
            />
            <p className="text-xs text-surface-500 mt-1">
              Domain-specific guidance for the extraction pipeline.
            </p>
          </div>

          {error && (
            <div className="rounded-md bg-error/10 border border-error/30 px-3 py-2 text-sm text-error flex items-center gap-2">
              <AlertCircle size={14} />
              {error}
            </div>
          )}

          <div className="flex justify-end gap-3 pt-2 border-t border-surface-800">
            <button type="button" onClick={onClose} className="btn-secondary text-sm" disabled={saving}>
              Cancel
            </button>
            <button type="submit" disabled={saving} className="btn-primary text-sm min-w-[140px] justify-center">
              {saving ? (
                <span className="flex items-center gap-2">
                  <Spinner /> Saving...
                </span>
              ) : (
                "Save Changes"
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ─── Delete Dialog ─────────────────────────────────────────────────────────────

interface DeleteDialogProps {
  instructionName: string;
  onClose: () => void;
  onConfirm: () => Promise<void>;
}

function DeleteDialog({ instructionName, onClose, onConfirm }: DeleteDialogProps) {
  const [submitting, setSubmitting] = useState(false);

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
            <h2 className="text-lg font-semibold">Delete Instruction</h2>
            <p className="text-sm text-surface-400">This action cannot be undone.</p>
          </div>
        </div>

        <p className="text-sm text-surface-300 mb-2">
          Are you sure you want to delete instruction
        </p>
        <p className="text-sm font-medium text-white mb-5">
          &ldquo;{instructionName}&rdquo;
        </p>
        <p className="text-xs text-surface-500 mb-5">
          This instruction will be removed from the extraction pipeline.
        </p>

        <div className="flex justify-end gap-3">
          <button onClick={onClose} className="btn-secondary text-sm" disabled={submitting}>
            Cancel
          </button>
          <button
            onClick={async () => { setSubmitting(true); try { await onConfirm(); } finally { setSubmitting(false); } }}
            disabled={submitting}
            className="btn-danger text-sm min-w-[100px] justify-center"
          >
            {submitting ? (
              <span className="flex items-center gap-2"><Spinner /> Deleting...</span>
            ) : (
              <span className="flex items-center gap-2"><Trash2 size={14} /> Delete</span>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Main Page ─────────────────────────────────────────────────────────────────

export default function ExtractionInstructionsPage() {
  const [instructions, setInstructions] = useState<CustomInstruction[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<CustomInstruction | null>(null);

  const [toast, setToast] = useState<ToastState>({ visible: false, message: "", type: "success" });

  const showToast = useCallback((message: string, type: "success" | "error") => {
    setToast({ visible: true, message, type });
  }, []);

  const dismissToast = useCallback(() => {
    setToast((prev) => ({ ...prev, visible: false }));
  }, []);

  // ── Fetch ──────────────────────────────────────────────────────────────────

  const fetchInstructions = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/admin/org/custom-instructions`, {
        headers: authHeaders(),
      });
      if (!res.ok) throw new Error("Failed to load custom instructions");
      const json = await res.json();
      setInstructions(json.data ?? []);
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Failed to load instructions", "error");
    } finally {
      setLoading(false);
    }
  }, [showToast]);

  useEffect(() => {
    fetchInstructions();
  }, [fetchInstructions]);

  // ── Optimistic PUT helper ──────────────────────────────────────────────────

  const putInstructions = async (
    newInstructions: CustomInstruction[],
    rollbackInstructions: CustomInstruction[],
  ): Promise<void> => {
    // Optimistic update
    setInstructions(newInstructions);
    try {
      const res = await fetch(`${API_BASE}/admin/org/custom-instructions`, {
        method: "PUT",
        headers: authHeaders(),
        body: JSON.stringify({ instructions: newInstructions }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail ?? "Failed to update instructions");
      }
      const json = await res.json();
      // Sync with server response
      setInstructions(json.data ?? newInstructions);
    } catch (err) {
      // Rollback on failure
      setInstructions(rollbackInstructions);
      throw err;
    }
  };

  // ── Create ─────────────────────────────────────────────────────────────────

  const handleCreate = async (name: string, text: string) => {
    const newInstruction: CustomInstruction = { name, text };
    const rollback = [...instructions];
    const updated = [...instructions, newInstruction];
    try {
      await putInstructions(updated, rollback);
      setShowCreate(false);
      showToast(`Instruction "${name}" added`, "success");
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Failed to add instruction", "error");
    }
  };

  // ── Edit ───────────────────────────────────────────────────────────────────

  const handleSave = async (name: string, text: string) => {
    if (editingIndex === null) return;
    const rollback = [...instructions];
    const updated = instructions.map((inst, idx) =>
      idx === editingIndex ? { name, text } : inst,
    );
    try {
      await putInstructions(updated, rollback);
      setEditingIndex(null);
      showToast(`Instruction "${name}" updated`, "success");
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Failed to update instruction", "error");
    }
  };

  // ── Delete ─────────────────────────────────────────────────────────────────

  const handleDelete = async () => {
    if (!deleteTarget) return;
    const rollback = [...instructions];
    const updated = instructions.filter((inst) => inst !== deleteTarget);
    try {
      await putInstructions(updated, rollback);
      setDeleteTarget(null);
      showToast(`Instruction "${deleteTarget.name}" deleted`, "success");
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Failed to delete instruction", "error");
    }
  };

  // ── Helpers ────────────────────────────────────────────────────────────────

  function truncateText(text: string, maxLen = 80): string {
    return text.length > maxLen ? text.slice(0, maxLen) + "…" : text;
  }

  // ── Skeleton rows ──────────────────────────────────────────────────────────

  const skeletonRows = Array.from({ length: 3 }, (_, i) => (
    <tr key={`skel-${i}`} className={i % 2 === 0 ? "bg-surface-950/50" : ""}>
      {[1, 2, 3].map((col) => (
        <td key={col} className="px-4 py-3">
          <div
            className="h-4 rounded bg-surface-800 animate-pulse"
            style={{ width: col === 1 ? "160px" : col === 2 ? "240px" : "100px" }}
          />
        </td>
      ))}
    </tr>
  ));

  // ── Empty state ────────────────────────────────────────────────────────────

  const emptyRow = (
    <tr>
      <td colSpan={3}>
        <div className="flex flex-col items-center justify-center py-16 text-surface-500">
          <FileText size={40} className="mb-3 opacity-40" />
          <p className="text-sm font-medium">No custom instructions set</p>
          <p className="text-xs mt-1 max-w-md text-center">
            Your extraction pipeline will use default prompts without domain-specific guidance.
          </p>
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
          <h1 className="text-2xl font-bold tracking-tight">Extraction Instructions</h1>
          <p className="text-sm text-surface-400 mt-1">
            Manage domain-specific instruction pairs for the extraction pipeline
          </p>
        </div>
        <button onClick={() => setShowCreate(true)} className="btn-primary text-sm">
          <Plus size={16} />
          Add Instruction
        </button>
      </div>

      {/* Table */}
      <div className="card-base overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-surface-800">
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Name</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Text</th>
                <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-surface-400">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-surface-800">
              {loading
                ? skeletonRows
                : instructions.length === 0
                  ? emptyRow
                  : instructions.map((inst, idx) => (
                      <tr
                        key={`inst-${idx}`}
                        className={cn(
                          "transition-colors hover:bg-surface-800/50",
                          idx % 2 === 0 ? "bg-surface-950/50" : "",
                        )}
                      >
                        {/* Name */}
                        <td className="px-4 py-3">
                          <span className="font-medium text-white">{inst.name}</span>
                        </td>

                        {/* Text (truncated to 80 chars) */}
                        <td className="px-4 py-3 max-w-[400px]">
                          <span className="text-xs text-surface-400 block" title={inst.text}>
                            {truncateText(inst.text, 80)}
                          </span>
                        </td>

                        {/* Actions */}
                        <td className="px-4 py-3 text-right">
                          <div className="flex items-center justify-end gap-1">
                            <button
                              onClick={() => setEditingIndex(idx)}
                              className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-white"
                              title="Edit instruction"
                            >
                              <Pencil size={14} />
                            </button>
                            <button
                              onClick={() => setDeleteTarget(inst)}
                              className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-error"
                              title="Delete instruction"
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

      {/* ── Edit Dialog ───────────────────────────────────────────────────────── */}
      {editingIndex !== null && instructions[editingIndex] && (
        <EditDialog
          instruction={instructions[editingIndex]}
          onClose={() => setEditingIndex(null)}
          onSave={handleSave}
        />
      )}

      {/* ── Delete Dialog ─────────────────────────────────────────────────────── */}
      {deleteTarget && (
        <DeleteDialog
          instructionName={deleteTarget.name}
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
