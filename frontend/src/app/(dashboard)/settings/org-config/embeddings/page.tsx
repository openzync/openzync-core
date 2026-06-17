"use client";
import { RequireAuth } from "../../../require-auth";

import { useEffect, useState, useCallback } from "react";
import { toast } from "sonner";
import { Save, Eye, EyeOff, AudioWaveform, X } from "lucide-react";
import { get, patch, ApiError } from "@/lib/api-client";
import { ErrorState } from "@/components/shared/error-state";
import { Button } from "@/components/ui/button";

// ─── Types ─────────────────────────────────────────────────────────────────────

interface OrgConfigResponse {
  stored: Record<string, unknown>;
}

type EmbeddingBackend = "openai" | "ollama" | "huggingface" | "sentence_transformers";

interface FormState {
  embedding_backend: EmbeddingBackend;
  embedding_model: string;
  embedding_dim: number;
  embedding_api_key: string;
  embedding_provider: string;
}

// ─── Constants ─────────────────────────────────────────────────────────────────

const FIELDS: (keyof FormState)[] = [
  "embedding_backend",
  "embedding_model",
  "embedding_dim",
  "embedding_api_key",
  "embedding_provider",
];

const BACKEND_OPTIONS: { value: EmbeddingBackend; label: string }[] = [
  { value: "openai", label: "OpenAI" },
  { value: "ollama", label: "Ollama" },
  { value: "huggingface", label: "Hugging Face" },
  { value: "sentence_transformers", label: "Sentence Transformers" },
];

// ─── Main Page ─────────────────────────────────────────────────────────────────

export default function EmbeddingsConfigPage() {
  const [form, setForm] = useState<FormState>({
    embedding_backend: "openai",
    embedding_model: "",
    embedding_dim: 1536,
    embedding_api_key: "",
    embedding_provider: "",
  });
  const [initialForm, setInitialForm] = useState<FormState>({ ...form });
  const [stored, setStored] = useState<Record<string, unknown>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showApiKey, setShowApiKey] = useState(false);

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
        defaults = await get<Record<string, unknown>>("/admin/org/config/defaults").catch(
          () => ({} as Record<string, unknown>),
        );
      }

      const val = (field: string, fallback: unknown) =>
        (stored[field] as unknown) ?? (defaults[field] as unknown) ?? fallback;

      setForm({
        embedding_backend: val("embedding_backend", "openai") as EmbeddingBackend,
        embedding_model: val("embedding_model", "") as string,
        embedding_dim: val("embedding_dim", 1536) as number,
        embedding_api_key: val("embedding_api_key", "") as string,
        embedding_provider: val("embedding_provider", "") as string,
      });
      setInitialForm({
        embedding_backend: val("embedding_backend", "openai") as EmbeddingBackend,
        embedding_model: val("embedding_model", "") as string,
        embedding_dim: val("embedding_dim", 1536) as number,
        embedding_api_key: val("embedding_api_key", "") as string,
        embedding_provider: val("embedding_provider", "") as string,
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
      await patch("/admin/org/config", { [field]: null });
      toast.success(`"${field}" reset to default`);
      await fetchConfig();
    } catch (err) {
      const message = err instanceof ApiError ? err.message : "Failed to reset field";
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
      toast.success("Embedding configuration saved successfully");
      await fetchConfig();
    } catch (err) {
      const message = err instanceof ApiError ? err.message : "Failed to save configuration";
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
      {/* ── Embedding Backend Card ────────────────────────────────────────────── */}
      <div className="card-base p-6">
        <div className="flex items-center gap-3 mb-6">
          <div className="flex h-10 w-10 items-center justify-center rounded-full bg-brand-500/10">
            <AudioWaveform size={20} className="text-brand-300" />
          </div>
          <div>
            <h2 className="text-base font-semibold">Embedding Provider</h2>
            <p className="text-xs text-surface-400">Vector embedding model configuration</p>
          </div>
        </div>

        {loading ? (
          <div className="space-y-4">
            <div className="h-9 rounded bg-surface-800 animate-pulse w-full" />
            <div className="h-9 rounded bg-surface-800 animate-pulse w-full" />
            <div className="h-9 rounded bg-surface-800 animate-pulse w-full" />
            <div className="h-9 rounded bg-surface-800 animate-pulse w-48" />
          </div>
        ) : (
          <>
            {error && <ErrorState message={error} onRetry={fetchConfig} />}
            <div className="space-y-4 max-w-md">
              {/* embedding_backend */}
              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1">
                  Backend Provider
                  
                </label>
                <div className="flex gap-2 items-start">
                  <select
                    className="input-base flex-1"
                    value={form.embedding_backend}
                    onChange={(e) => updateField("embedding_backend", e.target.value as EmbeddingBackend)}
                  >
                    {BACKEND_OPTIONS.map((opt) => (
                      <option key={opt.value} value={opt.value}>{opt.label}</option>
                    ))}
                  </select>
                  {isFieldSet("embedding_backend") && (
                    <button
                      onClick={() => handleResetField("embedding_backend")}
                      className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-brand-300 shrink-0 mt-0.5"
                      title="Reset to default"
                    >
                      <X size={14} />
                    </button>
                  )}
                </div>
              </div>

              {/* embedding_model */}
              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1">
                  Model
                  
                </label>
                <div className="flex gap-2 items-start">
                  <input
                    className="input-base flex-1"
                    placeholder="text-embedding-3-small, ..."
                    value={form.embedding_model}
                    onChange={(e) => updateField("embedding_model", e.target.value)}
                  />
                  {isFieldSet("embedding_model") && (
                    <button
                      onClick={() => handleResetField("embedding_model")}
                      className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-brand-300 shrink-0 mt-0.5"
                      title="Reset to default"
                    >
                      <X size={14} />
                    </button>
                  )}
                </div>
              </div>

              {/* embedding_dim */}
              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1">
                  Embedding Dimensions
                  
                </label>
                <div className="flex gap-2 items-start">
                  <input
                    className="input-base flex-1"
                    type="number"
                    min="64"
                    max="4096"
                    value={form.embedding_dim}
                    onChange={(e) => updateField("embedding_dim", parseInt(e.target.value) || 0)}
                  />
                  {isFieldSet("embedding_dim") && (
                    <button
                      onClick={() => handleResetField("embedding_dim")}
                      className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-brand-300 shrink-0 mt-0.5"
                      title="Reset to default"
                    >
                      <X size={14} />
                    </button>
                  )}
                </div>
              </div>

              {/* embedding_provider */}
              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1">
                  Provider Name
                  
                </label>
                <div className="flex gap-2 items-start">
                  <input
                    className="input-base flex-1"
                    placeholder="openai, azure, ..."
                    value={form.embedding_provider}
                    onChange={(e) => updateField("embedding_provider", e.target.value)}
                  />
                  {isFieldSet("embedding_provider") && (
                    <button
                      onClick={() => handleResetField("embedding_provider")}
                      className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-brand-300 shrink-0 mt-0.5"
                      title="Reset to default"
                    >
                      <X size={14} />
                    </button>
                  )}
                </div>
              </div>

              {/* embedding_api_key */}
              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1">
                  API Key
                  
                </label>
                <div className="flex gap-2 items-start">
                  <div className="relative flex-1">
                    <input
                      className="input-base pr-10 w-full"
                      type={showApiKey ? "text" : "password"}
                      placeholder="Embedding provider API key"
                      value={form.embedding_api_key}
                      onChange={(e) => updateField("embedding_api_key", e.target.value)}
                    />
                    <button
                      type="button"
                      onClick={() => setShowApiKey((prev) => !prev)}
                      className="absolute right-2.5 top-1/2 -translate-y-1/2 text-surface-500 hover:text-surface-300"
                    >
                      {showApiKey ? <EyeOff size={16} /> : <Eye size={16} />}
                    </button>
                  </div>
                  {isFieldSet("embedding_api_key") && (
                    <button
                      onClick={() => handleResetField("embedding_api_key")}
                      className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-brand-300 shrink-0 mt-0.5"
                      title="Reset to default"
                    >
                      <X size={14} />
                    </button>
                  )}
                </div>
              </div>
            </div>
          </>
        )}
      </div>

      {/* ── Save Button ───────────────────────────────────────────────────────── */}
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
            <span className="text-xs text-surface-500">No changes to save</span>
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
  </RequireAuth>
  );
}
