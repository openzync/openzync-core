"use client";

import { useEffect, useState, useCallback } from "react";
import { Save, X, Settings2 } from "lucide-react";
import { toast } from "sonner";
import { get, patch, ApiError } from "@/lib/api-client";
import { ErrorState } from "@/components/shared/error-state";
import { Button } from "@/components/ui/button";

// ─── Types ─────────────────────────────────────────────────────────────────────

interface OrgConfigResponse {
  stored: Record<string, unknown>;
}

interface FormState {
  context_cache_ttl: number;
  audit_log_response_body: boolean;
}

const FIELDS: (keyof FormState)[] = [
  "context_cache_ttl",
  "audit_log_response_body",
];

// ─── Main Page ─────────────────────────────────────────────────────────────────

export default function BehaviourConfigPage() {
  const [form, setForm] = useState<FormState>({
    context_cache_ttl: 1800,
    audit_log_response_body: true,
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

      const stored = data.stored as Record<string, unknown>;
      const hasAnyStored = FIELDS.some((f) => stored[f] != null);

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
        (stored[field] as unknown) ?? (defaults[field] as unknown) ?? fallback;

      setForm({
        context_cache_ttl: val("context_cache_ttl", 1800) as number,
        audit_log_response_body: val(
          "audit_log_response_body",
          true,
        ) as boolean,
      });
      setInitialForm({
        context_cache_ttl: val("context_cache_ttl", 1800) as number,
        audit_log_response_body: val(
          "audit_log_response_body",
          true,
        ) as boolean,
      });
      setStored(data.stored ?? {});
      setError(null);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load configuration",
      );
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

  function updateField<K extends keyof FormState>(
    field: K,
    value: FormState[K],
  ) {
    setForm((prev) => ({ ...prev, [field]: value }));
  }

  // ── Reset field to default ────────────────────────────────────────────────

  async function handleResetField(field: keyof FormState) {
    try {
      await patch("/admin/org/config", { [field]: null });
      toast.success(`"${field}" reset to default`);
      await fetchConfig();
    } catch (err) {
      toast.error(
        err instanceof ApiError
          ? err.message
          : "Failed to reset field",
      );
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

      toast.success("Behaviour configuration saved successfully");
      await fetchConfig();
    } catch (err) {
      const message =
        err instanceof Error ? err.message : "Failed to save configuration";
      setError(message);
      toast.error(
        err instanceof ApiError
          ? err.message
          : "Failed to save configuration",
      );
    } finally {
      setSaving(false);
    }
  }

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
      <div className="space-y-6">
        {/* ── Behaviour Configuration Card ──────────────────────────────────────── */}
        <div className="card-base p-6">
          <div className="flex items-center gap-3 mb-6">
            <div className="flex h-10 w-10 items-center justify-center rounded-full bg-brand-500/10">
              <Settings2 size={20} className="text-brand-300" />
            </div>
            <div>
              <h2 className="text-base font-semibold">Behaviour Settings</h2>
              <p className="text-xs text-surface-400">
                Caching and audit behaviour
              </p>
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
                <ErrorState message={error} onRetry={fetchConfig} />
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
                      onChange={(e) =>
                        updateField(
                          "context_cache_ttl",
                          parseInt(e.target.value) || 0,
                        )
                      }
                    />
                    {isFieldSet("context_cache_ttl") && (
                      <Button
                        onClick={() =>
                          handleResetField("context_cache_ttl")
                        }
                        variant="ghost" size="sm" className="rounded-md text-surface-400 hover:text-brand-300 shrink-0 mt-0.5"
                        title="Reset to default"
                      >
                        <X size={14} />
                      </Button>
                    )}
                  </div>
                  <p className="text-xs text-surface-500 mt-1">
                    How long context data is cached in Redis before being
                    re-fetched (0 = no caching)
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
                          onChange={(e) =>
                            updateField(
                              "audit_log_response_body",
                              e.target.checked,
                            )
                          }
                        />
                        <span className="text-sm text-surface-300">
                          {form.audit_log_response_body
                            ? "Enabled"
                            : "Disabled"}
                        </span>
                      </label>
                      {isFieldSet("audit_log_response_body") && (
                        <Button
                          onClick={() =>
                            handleResetField("audit_log_response_body")
                          }
                          variant="ghost" size="sm" className="rounded-md text-surface-400 hover:text-brand-300"
                          title="Reset to default"
                        >
                          <X size={14} />
                        </Button>
                      )}
                    </div>
                  </div>
                </div>
              </div>
            </>
          )}
        </div>

        {/* ── Save / Discard Buttons ────────────────────────────────────────────── */}
        {!loading && (
          <div className="flex items-center gap-3">
            <Button
              variant="primary"
              size="sm"
              icon={<Save size={14} />}
              loading={saving}
              disabled={saving || !hasChanged()}
              onClick={handleSave}
            >
              {saving ? "Saving..." : "Save Changes"}
            </Button>
            {!hasChanged() && (
              <span className="text-xs text-surface-500">
                No changes to save
              </span>
            )}
            {hasChanged() && (
              <Button
                variant="secondary"
                size="sm"
                onClick={() => setForm({ ...initialForm })}
              >
                Discard Changes
              </Button>
            )}
          </div>
        )}
      </div>
  );
}
