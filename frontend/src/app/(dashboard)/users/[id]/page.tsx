"use client";

import { RequireAuth } from "../../require-auth";

import { useEffect, useState, useCallback, useRef } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import {
  ArrowLeft,
  ArrowUpRight,
  Copy,
  Check,
  Calendar,
  Hash,
  User as UserIcon,
  Mail,
  Shield,
  MessageSquare,
  Database,
  Layers,
  FileText,
  Sparkles,
  RefreshCw,
  Plus,
  Edit,
  Trash2,
  X,
  CheckCircle,
  AlertCircle,
  BookOpen,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { API_BASE } from "@/lib/api-client";

// ─── Types ─────────────────────────────────────────────────────────────────────

interface UserWithStats {
  id: string;
  organization_id: string;
  external_id: string;
  name: string | null;
  email: string | null;
  role: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  message_count: number;
  fact_count: number;
  session_count: number;
}

interface UserSummaryResponse {
  user_id: string;
  summary: string | null;
  updated_at: string | null;
}

interface CustomInstruction {
  name: string;
  text: string;
}

interface InstructionsResponse {
  data: CustomInstruction[];
}

interface ToastState {
  visible: boolean;
  message: string;
  type: "success" | "error";
}

// ─── Constants ─────────────────────────────────────────────────────────────────

const POLL_INTERVAL_MS = 5000;
const POLL_TIMEOUT_MS = 120_000; // 2 minutes

// ─── Helpers ───────────────────────────────────────────────────────────────────

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (typeof window !== "undefined") {
    const token = sessionStorage.getItem("mg_access_token");
    if (token) headers["Authorization"] = `Bearer ${token}`;
  }
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

function formatDateTime(dateStr: string): string {
  const d = new Date(dateStr);
  return d.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function shortId(id: string): string {
  if (id.length <= 12) return id;
  return `${id.slice(0, 6)}\u2026${id.slice(-4)}`;
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

// ─── Copy Button ───────────────────────────────────────────────────────────────

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard API not available
    }
  }

  return (
    <button
      onClick={handleCopy}
      className="text-surface-500 hover:text-surface-300 transition-colors shrink-0"
      title="Copy to clipboard"
    >
      {copied ? <Check size={14} className="text-success" /> : <Copy size={14} />}
    </button>
  );
}

// ─── Metadata Row ──────────────────────────────────────────────────────────────

function MetadataRow({
  icon,
  label,
  children,
}: {
  icon: React.ReactNode;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-start gap-3">
      <div className="mt-0.5 text-surface-500 shrink-0">{icon}</div>
      <div className="min-w-0 flex-1">
        <div className="text-xs text-surface-500 mb-0.5">{label}</div>
        <div className="text-sm text-surface-200">{children}</div>
      </div>
    </div>
  );
}

// ─── Stat Card ─────────────────────────────────────────────────────────────────

function StatCard({
  icon,
  label,
  value,
}: {
  icon: React.ReactNode;
  label: string;
  value: number;
}) {
  return (
    <div className="flex items-center gap-3 rounded-lg bg-surface-800/50 border border-surface-700/50 px-4 py-3">
      <div className="text-surface-500 shrink-0">{icon}</div>
      <div>
        <div className="text-xs text-surface-500">{label}</div>
        <div className="text-lg font-semibold text-white">{value.toLocaleString()}</div>
      </div>
    </div>
  );
}

// ─── Instruction Create Dialog ─────────────────────────────────────────────────

interface InstructionCreateDialogProps {
  onClose: () => void;
  onCreate: (name: string, text: string) => Promise<void>;
}

function InstructionCreateDialog({ onClose, onCreate }: InstructionCreateDialogProps) {
  const [name, setName] = useState("");
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmedName = name.trim();
    const trimmedText = text.trim();
    if (!trimmedName) { setError("Instruction name is required"); return; }
    if (!trimmedText) { setError("Instruction text is required"); return; }

    setSubmitting(true);
    setError(null);
    try {
      await onCreate(trimmedName, trimmedText);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create instruction");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 animate-fade-in" onClick={onClose}>
      <div
        className="card-base w-full max-w-lg p-6 shadow-xl shadow-black/40 animate-slide-up"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">Add Summary Instruction</h2>
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
              placeholder="e.g. Tone & Voice"
              value={name}
              onChange={(e) => { setName(e.target.value); if (error) setError(null); }}
              autoFocus
              disabled={submitting}
            />
            <p className="text-xs text-surface-500 mt-1">A label for this instruction.</p>
          </div>

          {/* Text */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">
              Instruction Text <span className="text-error">*</span>
            </label>
            <textarea
              className="input-base min-h-[120px] resize-y"
              placeholder="e.g. Summarize the user's tone, communication style, and preferred vocabulary."
              value={text}
              onChange={(e) => { setText(e.target.value); if (error) setError(null); }}
              disabled={submitting}
            />
            <p className="text-xs text-surface-500 mt-1">
              The instruction given to the LLM when generating the summary.
            </p>
          </div>

          {error && (
            <div className="rounded-md bg-error/10 border border-error/30 px-3 py-2 text-sm text-error flex items-center gap-2">
              <AlertCircle size={14} />
              {error}
            </div>
          )}

          <div className="flex justify-end gap-3 pt-2 border-t border-surface-800">
            <button type="button" onClick={onClose} className="btn-secondary text-sm" disabled={submitting}>
              Cancel
            </button>
            <button type="submit" disabled={submitting} className="btn-primary text-sm min-w-[140px] justify-center">
              {submitting ? (
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

// ─── Instruction Edit Dialog ───────────────────────────────────────────────────

interface InstructionEditDialogProps {
  initial: CustomInstruction;
  onClose: () => void;
  onSave: (name: string, text: string) => Promise<void>;
}

function InstructionEditDialog({ initial, onClose, onSave }: InstructionEditDialogProps) {
  const [name, setName] = useState(initial.name);
  const [text, setText] = useState(initial.text);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmedName = name.trim();
    const trimmedText = text.trim();
    if (!trimmedName) { setError("Instruction name is required"); return; }
    if (!trimmedText) { setError("Instruction text is required"); return; }

    setSubmitting(true);
    setError(null);
    try {
      await onSave(trimmedName, trimmedText);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update instruction");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 animate-fade-in" onClick={onClose}>
      <div
        className="card-base w-full max-w-lg p-6 shadow-xl shadow-black/40 animate-slide-up"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">Edit Summary Instruction</h2>
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
              placeholder="e.g. Tone & Voice"
              value={name}
              onChange={(e) => { setName(e.target.value); if (error) setError(null); }}
              autoFocus
              disabled={submitting}
            />
          </div>

          {/* Text */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">
              Instruction Text <span className="text-error">*</span>
            </label>
            <textarea
              className="input-base min-h-[120px] resize-y"
              value={text}
              onChange={(e) => { setText(e.target.value); if (error) setError(null); }}
              disabled={submitting}
            />
          </div>

          {error && (
            <div className="rounded-md bg-error/10 border border-error/30 px-3 py-2 text-sm text-error flex items-center gap-2">
              <AlertCircle size={14} />
              {error}
            </div>
          )}

          <div className="flex justify-end gap-3 pt-2 border-t border-surface-800">
            <button type="button" onClick={onClose} className="btn-secondary text-sm" disabled={submitting}>
              Cancel
            </button>
            <button type="submit" disabled={submitting} className="btn-primary text-sm min-w-[120px] justify-center">
              {submitting ? (
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

// ─── Instruction Delete Dialog ─────────────────────────────────────────────────

interface InstructionDeleteDialogProps {
  instructionName: string;
  onClose: () => void;
  onConfirm: () => Promise<void>;
}

function InstructionDeleteDialog({ instructionName, onClose, onConfirm }: InstructionDeleteDialogProps) {
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
          This instruction will no longer be used when generating user summaries.
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

export default function UserDetailPage() {
  const params = useParams();
  const router = useRouter();
  const userId = params.id as string;

  // ── Data state ────────────────────────────────────────────────────────────
  const [user, setUser] = useState<UserWithStats | null>(null);
  const [summary, setSummary] = useState<UserSummaryResponse | null>(null);
  const [instructions, setInstructions] = useState<CustomInstruction[]>([]);
  const [loading, setLoading] = useState(true);

  // ── Summary generation state ──────────────────────────────────────────────
  const [generating, setGenerating] = useState(false);
  const [polling, setPolling] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pollStartRef = useRef<number>(0);
  const initialUpdatedAtRef = useRef<string | null>(null);

  // ── Instruction dialog state ──────────────────────────────────────────────
  const [showCreateInstruction, setShowCreateInstruction] = useState(false);
  const [editInstructionTarget, setEditInstructionTarget] = useState<CustomInstruction | null>(null);
  const [deleteInstructionTarget, setDeleteInstructionTarget] = useState<CustomInstruction | null>(null);

  // ── Toast state ───────────────────────────────────────────────────────────
  const [toast, setToast] = useState<ToastState>({ visible: false, message: "", type: "success" });

  const showToast = useCallback((message: string, type: "success" | "error") => {
    setToast({ visible: true, message, type });
  }, []);

  const dismissToast = useCallback(() => {
    setToast((prev) => ({ ...prev, visible: false }));
  }, []);

  // ── Cleanup polling on unmount ────────────────────────────────────────────
  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, []);

  // ── Fetch all data on mount ───────────────────────────────────────────────
  const fetchData = useCallback(async () => {
    if (!userId) return;
    setLoading(true);

    try {
      const [userRes, summaryRes, instructionsRes] = await Promise.all([
        fetch(`${API_BASE}/v1/users/${userId}`, { headers: authHeaders() }),
        fetch(`${API_BASE}/v1/users/${userId}/summary`, { headers: authHeaders() }),
        fetch(`${API_BASE}/v1/users/${userId}/summary-instructions`, { headers: authHeaders() }),
      ]);

      if (!userRes.ok) {
        if (userRes.status === 404) {
          showToast("User not found", "error");
          router.replace("/users");
          return;
        }
        throw new Error(`Failed to load user: ${userRes.status}`);
      }

      const userData: UserWithStats = await userRes.json();
      setUser(userData);

      if (summaryRes.ok) {
        const summaryData: UserSummaryResponse = await summaryRes.json();
        setSummary(summaryData);
      }
      // Silently swallow 404 for summary (not yet generated)

      if (instructionsRes.ok) {
        const instructionsData: InstructionsResponse = await instructionsRes.json();
        setInstructions(instructionsData.data ?? []);
      }
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Failed to load user data", "error");
    } finally {
      setLoading(false);
    }
  }, [userId, showToast, router]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  // ── Generate summary ──────────────────────────────────────────────────────

  const handleGenerateSummary = async () => {
    setGenerating(true);
    try {
      const res = await fetch(`${API_BASE}/v1/users/${userId}/summary`, {
        method: "POST",
        headers: authHeaders(),
      });

      if (res.status === 202) {
        showToast("Summary generation started. Check back in a few moments.", "success");
        // Start polling
        initialUpdatedAtRef.current = summary?.updated_at ?? null;
        pollStartRef.current = Date.now();
        setPolling(true);

        pollRef.current = setInterval(async () => {
          try {
            const pollRes = await fetch(`${API_BASE}/v1/users/${userId}/summary`, {
              headers: authHeaders(),
            });
            if (pollRes.ok) {
              const pollData: UserSummaryResponse = await pollRes.json();
              if (pollData.updated_at !== initialUpdatedAtRef.current) {
                // Summary has been regenerated — stop polling
                setSummary(pollData);
                setPolling(false);
                if (pollRef.current) clearInterval(pollRef.current);
                pollRef.current = null;
              }
            }
          } catch {
            // Silently swallow poll errors — we'll retry next interval
          }

          // Timeout check
          if (Date.now() - pollStartRef.current >= POLL_TIMEOUT_MS) {
            setPolling(false);
            if (pollRef.current) clearInterval(pollRef.current);
            pollRef.current = null;
            showToast("Summary generation is taking longer than expected. Refresh the page to check.", "error");
          }
        }, POLL_INTERVAL_MS);
      } else if (res.status === 429) {
        showToast("Please wait 5 minutes between summary generations.", "error");
      } else {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail ?? `Failed to generate summary (${res.status})`);
      }
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Failed to generate summary", "error");
    } finally {
      setGenerating(false);
    }
  };

  // ── Summary instructions CRUD ─────────────────────────────────────────────

  const syncInstructions = async (updatedInstructions: CustomInstruction[]) => {
    const res = await fetch(`${API_BASE}/v1/users/${userId}/summary-instructions`, {
      method: "PUT",
      headers: authHeaders(),
      body: JSON.stringify({ instructions: updatedInstructions }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail ?? "Failed to update instructions");
    }
    const result: InstructionsResponse = await res.json();
    setInstructions(result.data ?? []);
  };

  const handleCreateInstruction = async (name: string, text: string) => {
    const updated = [...instructions, { name, text }];
    await syncInstructions(updated);
    setShowCreateInstruction(false);
    showToast(`Instruction "${name}" added successfully`, "success");
  };

  const handleEditInstruction = async (name: string, text: string) => {
    if (!editInstructionTarget) return;
    const updated = instructions.map((inst) =>
      inst.name === editInstructionTarget.name ? { name, text } : inst,
    );
    await syncInstructions(updated);
    setEditInstructionTarget(null);
    showToast(`Instruction "${name}" updated successfully`, "success");
  };

  const handleDeleteInstruction = async () => {
    if (!deleteInstructionTarget) return;
    const updated = instructions.filter(
      (inst) => inst.name !== deleteInstructionTarget.name,
    );
    await syncInstructions(updated);
    setDeleteInstructionTarget(null);
    showToast(`Instruction "${deleteInstructionTarget.name}" deleted`, "success");
  };

  // ── Loading skeleton ─────────────────────────────────────────────────────

  if (loading) {
    return (
      <RequireAuth>
        <div className="space-y-6">
          {/* Back button skeleton */}
          <button disabled className="btn-ghost text-xs -ml-2 opacity-50">
            <ArrowLeft size={14} />
            Back to Users
          </button>

          {/* Profile skeleton */}
          <div className="card-base p-6">
            <div className="space-y-4">
              <div className="h-6 w-48 rounded bg-surface-800 animate-pulse" />
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {[1, 2, 3, 4, 5, 6].map((i) => (
                  <div key={i} className="space-y-2">
                    <div className="h-3 w-16 rounded bg-surface-800 animate-pulse" />
                    <div className="h-4 w-32 rounded bg-surface-800 animate-pulse" />
                  </div>
                ))}
              </div>
              {/* Stats skeleton */}
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 pt-4 border-t border-surface-800">
                {[1, 2, 3].map((i) => (
                  <div key={i} className="h-16 rounded-lg bg-surface-800 animate-pulse" />
                ))}
              </div>
            </div>
          </div>

          {/* Summary skeleton */}
          <div className="card-base p-6">
            <div className="h-6 w-36 rounded bg-surface-800 animate-pulse mb-4" />
            <div className="h-20 rounded bg-surface-800 animate-pulse" />
          </div>

          {/* Instructions skeleton */}
          <div className="card-base p-6">
            <div className="h-6 w-48 rounded bg-surface-800 animate-pulse mb-4" />
            <div className="space-y-3">
              {[1, 2].map((i) => (
                <div key={i} className="h-16 rounded-lg bg-surface-800 animate-pulse" />
              ))}
            </div>
          </div>
        </div>
      </RequireAuth>
    );
  }

  // ── Render ───────────────────────────────────────────────────────────────

  return (
    <RequireAuth>
    <div className="space-y-6">
      {/* ═══ Back button ═══ */}
      <button
        onClick={() => router.push("/users")}
        className="btn-ghost text-xs -ml-2"
      >
        <ArrowLeft size={14} />
        Back to Users
      </button>

      {/* ═══ Section A: Profile header card ═══ */}
      <div className="card-base p-6">
        {user && (
          <>
            {/* Title row */}
            <div className="flex items-start justify-between mb-6">
              <div>
                <h1 className="text-xl font-bold tracking-tight">
                  {user.external_id}
                </h1>
                <p className="text-xs text-surface-500 mt-0.5">
                  {user.name ? `${user.name} \u2022 ` : ""}
                  User overview
                </p>
              </div>
              {/* Link to sessions list for this user */}
              <Link
                href={`/sessions?userId=${encodeURIComponent(user.id)}`}
                className="btn-ghost text-xs text-surface-400 hover:text-white gap-1.5"
              >
                <ArrowUpRight size={14} />
                Sessions
              </Link>
            </div>

            {/* Metadata grid */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
              {/* User ID */}
              <MetadataRow
                icon={<Hash size={16} />}
                label="User ID"
              >
                <div className="flex items-center gap-2">
                  <span className="font-mono text-xs bg-surface-800 rounded px-2 py-0.5">
                    {shortId(user.id)}
                  </span>
                  <CopyButton text={user.id} />
                </div>
              </MetadataRow>

              {/* External ID */}
              <MetadataRow
                icon={<UserIcon size={16} />}
                label="External ID"
              >
                <span className="font-mono text-xs">{user.external_id}</span>
              </MetadataRow>

              {/* Name */}
              <MetadataRow
                icon={<UserIcon size={16} />}
                label="Name"
              >
                {user.name || <span className="text-surface-500 italic">—</span>}
              </MetadataRow>

              {/* Email */}
              <MetadataRow
                icon={<Mail size={16} />}
                label="Email"
              >
                {user.email ? (
                  <span className="font-mono text-xs">{user.email}</span>
                ) : (
                  <span className="text-surface-500 italic">—</span>
                )}
              </MetadataRow>

              {/* Role */}
              <MetadataRow
                icon={<Shield size={16} />}
                label="Role"
              >
                <span className="capitalize">{user.role}</span>
              </MetadataRow>

              {/* Created */}
              <MetadataRow
                icon={<Calendar size={16} />}
                label="Created"
              >
                <span>{formatDateTime(user.created_at)}</span>
              </MetadataRow>
            </div>

            {/* Stats row */}
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mt-6 pt-6 border-t border-surface-800">
              <StatCard
                icon={<MessageSquare size={18} />}
                label="Messages"
                value={user.message_count}
              />
              <StatCard
                icon={<Database size={18} />}
                label="Facts"
                value={user.fact_count}
              />
              <StatCard
                icon={<Layers size={18} />}
                label="Sessions"
                value={user.session_count}
              />
            </div>
          </>
        )}
      </div>

      {/* ═══ Section B: Summary card ═══ */}
      <div className="card-base p-6">
        <div className="flex items-start justify-between mb-4">
          <div>
            <h2 className="text-lg font-semibold flex items-center gap-2">
              <FileText size={18} className="text-surface-400" />
              User Summary
            </h2>
            <p className="text-xs text-surface-500 mt-0.5">
              {summary?.updated_at
                ? `Last generated: ${formatDateTime(summary.updated_at)}`
                : "Not yet generated"}
            </p>
          </div>
          <div className="flex items-center gap-2">
            {/* Polling indicator */}
            {polling && (
              <span className="text-xs text-brand-400 flex items-center gap-1.5 animate-pulse">
                <RefreshCw size={12} className="animate-spin" />
                Refreshing...
              </span>
            )}
            {/* Generate button */}
            <button
              onClick={handleGenerateSummary}
              disabled={generating || polling}
              className="btn-primary text-sm"
            >
              {generating ? (
                <span className="flex items-center gap-2">
                  <Spinner /> Generating...
                </span>
              ) : (
                <span className="flex items-center gap-2">
                  <Sparkles size={14} />
                  Generate Summary
                </span>
              )}
            </button>
          </div>
        </div>

        {/* Summary content */}
        {summary?.summary ? (
          <div className="bg-surface-900/50 border border-surface-700/50 rounded-lg p-4 text-sm text-surface-200 leading-relaxed">
            <p className="whitespace-pre-wrap">{summary.summary}</p>
          </div>
        ) : (
          <div className="flex flex-col items-center justify-center py-10 text-surface-500">
            <FileText size={32} className="mb-3 opacity-40" />
            <p className="text-sm font-medium">No summary generated yet</p>
            <p className="text-xs mt-1 text-center max-w-md">
              Click &ldquo;Generate Summary&rdquo; to create a profile of this user based on their conversation history.
            </p>
          </div>
        )}
      </div>

      {/* ═══ Section C: Summary Instructions ═══ */}
      <div className="card-base p-6">
        <div className="flex items-start justify-between mb-4">
          <div>
            <h2 className="text-lg font-semibold flex items-center gap-2">
              <BookOpen size={18} className="text-surface-400" />
              Summary Instructions
            </h2>
            <p className="text-xs text-surface-500 mt-0.5">
              Custom instructions used when generating the user summary
            </p>
          </div>
          <button
            onClick={() => setShowCreateInstruction(true)}
            className="btn-primary text-sm"
          >
            <Plus size={14} />
            Add Instruction
          </button>
        </div>

        {instructions.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-10 text-surface-500">
            <BookOpen size={32} className="mb-3 opacity-40" />
            <p className="text-sm font-medium">No summary instructions</p>
            <p className="text-xs mt-1">
              Add custom instructions to guide how the user summary is generated.
            </p>
          </div>
        ) : (
          <div className="space-y-3">
            {instructions.map((inst, idx) => (
              <div
                key={`${inst.name}-${idx}`}
                className="flex items-start gap-4 rounded-lg border border-surface-700/50 bg-surface-900/30 p-4"
              >
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1">
                    <span className="text-sm font-medium text-white">{inst.name}</span>
                  </div>
                  <p className="text-xs text-surface-400 leading-relaxed whitespace-pre-wrap">
                    {inst.text}
                  </p>
                </div>
                <div className="flex items-center gap-1 shrink-0">
                  <button
                    onClick={() => setEditInstructionTarget(inst)}
                    className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-white"
                    title="Edit instruction"
                  >
                    <Edit size={14} />
                  </button>
                  <button
                    onClick={() => setDeleteInstructionTarget(inst)}
                    className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-error"
                    title="Delete instruction"
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* ═══ Dialogs ═══ */}

      {/* Create Instruction Dialog */}
      {showCreateInstruction && (
        <InstructionCreateDialog
          onClose={() => setShowCreateInstruction(false)}
          onCreate={handleCreateInstruction}
        />
      )}

      {/* Edit Instruction Dialog */}
      {editInstructionTarget && (
        <InstructionEditDialog
          initial={editInstructionTarget}
          onClose={() => setEditInstructionTarget(null)}
          onSave={handleEditInstruction}
        />
      )}

      {/* Delete Instruction Dialog */}
      {deleteInstructionTarget && (
        <InstructionDeleteDialog
          instructionName={deleteInstructionTarget.name}
          onClose={() => setDeleteInstructionTarget(null)}
          onConfirm={handleDeleteInstruction}
        />
      )}

      {/* ═══ Toast ═══ */}
      <Toast toast={toast} onDismiss={dismissToast} />
    </div>
  </RequireAuth>
  );
}
