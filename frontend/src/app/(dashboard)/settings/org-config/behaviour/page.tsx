"use client";
import { RequireAuth } from "../../../require-auth";

import { useEffect, useState, useCallback } from "react";
import {
  Save,
  CheckCircle,
  AlertCircle,
  X,
  Settings2,
} from "lucide-react";
import { cn } from "@/lib/utils";

// ─── Types ─────────────────────────────────────────────────────────────────────

interface OrgConfigResponse {
  stored: Record<string, unknown>;
}

interface FormState {
  context_cache_ttl: number;
  audit_log_response_body: boolean;
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
  "context_cache_ttl",
  "audit_log_response_body",
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
  context_cache_ttl: 1800,
  audit_log_response_body: true,
};

// ─── Main Page ─────────────────────────────────────────────────────────────────

export default function BehaviourConfigPage() {
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
        context_cache_ttl: (stored.context_cache_ttl as number) ?? 1800,
        audit_log_response_body: (stored.audit_log_response_body as boolean) ?? true,
      });
      setInitialForm({
        context_cache_ttl: (stored.context_cache_ttl as number) ?? 1800,
        audit_log_response_body: (stored.audit_log_response_body as boolean) ?? true,
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

      showToast("Behaviour configuration saved successfully", "success");
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
      {/* ── Behaviour Configuration Card ──────────────────────────────────────── */}
      <div className="card-base p-6">
        <div className="flex items-center gap-3 mb-6">
          <div className="flex h-10 w-10 items-center justify-center rounded-full bg-brand-500/10">
            <Settings2 size={20} className="text-brand-300" />
          </div>
          <div>
            <h2 className="text-base font-semibold">Behaviour Settings</h2>
            <p className="text-xs text-surface-400">Caching and audit behaviour</p>
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
              {/* context_cache_ttl */}
              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1">
                  Context Cache TTL (seconds)
                  
                </label>
                <div className="flex gap-2 items-start">
                  <input
                    className="input-base flex-1"
                    type="number"
                    min="0"
                    value={form.context_cache_ttl}
                    onChange={(e) => updateField("context_cache_ttl", parseInt(e.target.value) || 0)}
                  />
                  {isFieldSet("context_cache_ttl") && (
                    <button
                      onClick={() => handleResetField("context_cache_ttl")}
                      className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-brand-300 shrink-0 mt-0.5"
                      title="Reset to default"
                    >
                      <X size={14} />
                    </button>
                  )}
                </div>
                <p className="text-xs text-surface-500 mt-1">
                  How long context data is cached in Redis before being re-fetched (0 = no caching)
                </p>
              </div>

              {/* audit_log_response_body — checkbox toggle */}
              <div className="pt-2">
                <div className="flex items-start justify-between">
                  <div>
                    <label className="block text-sm font-medium text-surface-300 mb-1">
                      Audit Log Response Body
                      
                    </label>
                    <p className="text-xs text-surface-500">
                      Include response body content in audit logs
                    </p>
                  </div>
                  <div className="flex items-center gap-2 shrink-0 ml-4">
                    <label className="flex items-center gap-3 cursor-pointer">
                      <input
                        type="checkbox"
                        className="rounded border-surface-600 bg-surface-800 text-brand-500"
                        checked={form.audit_log_response_body}
                        onChange={(e) => updateField("audit_log_response_body", e.target.checked)}
                      />
                      <span className="text-sm text-surface-300">
                        {form.audit_log_response_body ? "Enabled" : "Disabled"}
                      </span>
                    </label>
                    {isFieldSet("audit_log_response_body") && (
                      <button
                        onClick={() => handleResetField("audit_log_response_body")}
                        className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-brand-300"
                        title="Reset to default"
                      >
                        <X size={14} />
                      </button>
                    )}
                  </div>
                </div>
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
