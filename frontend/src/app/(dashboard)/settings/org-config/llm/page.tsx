"use client";
import { RequireAuth } from "../../../require-auth";

import { useEffect, useState, useCallback } from "react";
import {
  Brain,
  Save,
  CheckCircle,
  AlertCircle,
  X,
  Eye,
  EyeOff,
} from "lucide-react";
import { cn } from "@/lib/utils";

// ─── Types ─────────────────────────────────────────────────────────────────────

interface OrgConfigResponse {
  stored: Record<string, unknown>;
}

type LlmBackend = "openai" | "anthropic" | "ollama" | "openai_like" | "openrouter" | "azure";

interface FormState {
  llm_backend: LlmBackend;
  llm_model: string;
  llm_temperature: number;
  llm_max_tokens: number;
  openai_api_key: string;
  anthropic_api_key: string;
  openrouter_api_key: string;
  ollama_base_url: string;
  azure_openai_endpoint: string;
  azure_openai_key: string;
}

interface ToastState {
  visible: boolean;
  message: string;
  type: "success" | "error";
}

// ─── Constants ─────────────────────────────────────────────────────────────────

const API_BASE = "http://localhost:8000";
const TOAST_DURATION = 4000;
const LLM_FIELDS: (keyof FormState)[] = [
  "llm_backend",
  "llm_model",
  "llm_temperature",
  "llm_max_tokens",
  "openai_api_key",
  "anthropic_api_key",
  "openrouter_api_key",
  "ollama_base_url",
  "azure_openai_endpoint",
  "azure_openai_key",
];

const BACKEND_OPTIONS: { value: LlmBackend; label: string }[] = [
  { value: "openai", label: "OpenAI" },
  { value: "anthropic", label: "Anthropic" },
  { value: "openrouter", label: "OpenRouter" },
  { value: "azure", label: "Azure OpenAI" },
  { value: "ollama", label: "Ollama" },
  { value: "openai_like", label: "OpenAI-compatible" },
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

// ─── Helpers ───────────────────────────────────────────────────────────────────

const DEFAULT_FORM: FormState = {
  llm_backend: "openai",
  llm_model: "",
  llm_temperature: 0.7,
  llm_max_tokens: 4096,
  openai_api_key: "",
  anthropic_api_key: "",
  openrouter_api_key: "",
  ollama_base_url: "",
  azure_openai_endpoint: "",
  azure_openai_key: "",
};

// ─── Main Page ─────────────────────────────────────────────────────────────────

export default function LlmConfigPage() {
  const [form, setForm] = useState<FormState>({ ...DEFAULT_FORM });
  const [initialForm, setInitialForm] = useState<FormState>({ ...DEFAULT_FORM });
  const [stored, setStored] = useState<Record<string, unknown>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Password visibility toggles
  const [showOpenAiKey, setShowOpenAiKey] = useState(false);
  const [showAnthropicKey, setShowAnthropicKey] = useState(false);
  const [showOpenRouterKey, setShowOpenRouterKey] = useState(false);
  const [showAzureKey, setShowAzureKey] = useState(false);

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
        llm_backend: (stored.llm_backend as LlmBackend) ?? "openai",
        llm_model: (stored.llm_model as string) ?? "",
        llm_temperature: (stored.llm_temperature as number) ?? 0.7,
        llm_max_tokens: (stored.llm_max_tokens as number) ?? 4096,
        openai_api_key: (stored.openai_api_key as string) ?? "",
        anthropic_api_key: (stored.anthropic_api_key as string) ?? "",
        openrouter_api_key: (stored.openrouter_api_key as string) ?? "",
        ollama_base_url: (stored.ollama_base_url as string) ?? "",
        azure_openai_endpoint: (stored.azure_openai_endpoint as string) ?? "",
        azure_openai_key: (stored.azure_openai_key as string) ?? "",
      });
      setInitialForm({
        llm_backend: (stored.llm_backend as LlmBackend) ?? "openai",
        llm_model: (stored.llm_model as string) ?? "",
        llm_temperature: (stored.llm_temperature as number) ?? 0.7,
        llm_max_tokens: (stored.llm_max_tokens as number) ?? 4096,
        openai_api_key: (stored.openai_api_key as string) ?? "",
        anthropic_api_key: (stored.anthropic_api_key as string) ?? "",
        openrouter_api_key: (stored.openrouter_api_key as string) ?? "",
        ollama_base_url: (stored.ollama_base_url as string) ?? "",
        azure_openai_endpoint: (stored.azure_openai_endpoint as string) ?? "",
        azure_openai_key: (stored.azure_openai_key as string) ?? "",
      });
      setStored(data.stored ?? {});
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load configuration");
      // initialForm keeps defaults so the form remains interactive
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
    return LLM_FIELDS.some((f) => form[f] !== initialForm[f]);
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
      for (const field of LLM_FIELDS) {
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

      showToast("LLM configuration saved successfully", "success");
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
      {/* ── LLM Backend Card ─────────────────────────────────────────────────── */}
      <div className="card-base p-6">
        <div className="flex items-center gap-3 mb-6">
          <div className="flex h-10 w-10 items-center justify-center rounded-full bg-brand-500/10">
            <Brain size={20} className="text-brand-300" />
          </div>
          <div>
            <h2 className="text-base font-semibold">LLM Backend</h2>
            <p className="text-xs text-surface-400">Primary language model provider</p>
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
            {error && (
              <div className="rounded-md bg-error/10 border border-error/30 px-3 py-2 mb-4 text-sm text-error flex items-center gap-2">
                <AlertCircle size={14} />
                {error}
              </div>
            )}
            <div className="space-y-4 max-w-md">
              {/* llm_backend */}
              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1">
                  Backend Provider
                  
                </label>
                <div className="flex gap-2 items-start">
                  <select
                    className="input-base flex-1"
                    value={form.llm_backend}
                    onChange={(e) => updateField("llm_backend", e.target.value as LlmBackend)}
                  >
                    {BACKEND_OPTIONS.map((opt) => (
                      <option key={opt.value} value={opt.value}>{opt.label}</option>
                    ))}
                  </select>
                  {isFieldSet("llm_backend") && (
                    <button
                      onClick={() => handleResetField("llm_backend")}
                      className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-brand-300 shrink-0 mt-0.5"
                      title="Reset to default"
                    >
                      <X size={14} />
                    </button>
                  )}
                </div>
              </div>

              {/* llm_model */}
              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1">
                  Model
                  
                </label>
                <div className="flex gap-2 items-start">
                  <input
                    className="input-base flex-1"
                    placeholder="gpt-4o, claude-sonnet-4, ..."
                    value={form.llm_model}
                    onChange={(e) => updateField("llm_model", e.target.value)}
                  />
                  {isFieldSet("llm_model") && (
                    <button
                      onClick={() => handleResetField("llm_model")}
                      className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-brand-300 shrink-0 mt-0.5"
                      title="Reset to default"
                    >
                      <X size={14} />
                    </button>
                  )}
                </div>
              </div>

              {/* llm_temperature */}
              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1">
                  Temperature
                  
                </label>
                <div className="flex gap-2 items-start">
                  <input
                    className="input-base flex-1"
                    type="number"
                    step="0.1"
                    min="0"
                    max="2"
                    value={form.llm_temperature}
                    onChange={(e) => updateField("llm_temperature", parseFloat(e.target.value) || 0)}
                  />
                  {isFieldSet("llm_temperature") && (
                    <button
                      onClick={() => handleResetField("llm_temperature")}
                      className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-brand-300 shrink-0 mt-0.5"
                      title="Reset to default"
                    >
                      <X size={14} />
                    </button>
                  )}
                </div>
              </div>

              {/* llm_max_tokens */}
              <div>
                <label className="block text-sm font-medium text-surface-300 mb-1">
                  Max Tokens
                  
                </label>
                <div className="flex gap-2 items-start">
                  <input
                    className="input-base flex-1"
                    type="number"
                    min="1"
                    value={form.llm_max_tokens}
                    onChange={(e) => updateField("llm_max_tokens", parseInt(e.target.value) || 0)}
                  />
                  {isFieldSet("llm_max_tokens") && (
                    <button
                      onClick={() => handleResetField("llm_max_tokens")}
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

      {/* ── API Keys Card ────────────────────────────────────────────────────── */}
      <div className="card-base p-6">
        <div className="flex items-center gap-3 mb-6">
          <div className="flex h-10 w-10 items-center justify-center rounded-full bg-warning/10">
            <Eye size={20} className="text-warning" />
          </div>
          <div>
            <h2 className="text-base font-semibold">LLM API Keys</h2>
            <p className="text-xs text-surface-400">Credentials for LLM providers</p>
          </div>
        </div>

        {!loading && (
          <div className="space-y-4 max-w-md">
            {/* openai_api_key */}
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1">
                OpenAI API Key
                
              </label>
              <div className="flex gap-2 items-start">
                <div className="relative flex-1">
                  <input
                    className="input-base pr-10 w-full"
                    type={showOpenAiKey ? "text" : "password"}
                    placeholder="sk-..."
                    value={form.openai_api_key}
                    onChange={(e) => updateField("openai_api_key", e.target.value)}
                  />
                  <button
                    type="button"
                    onClick={() => setShowOpenAiKey((prev) => !prev)}
                    className="absolute right-2.5 top-1/2 -translate-y-1/2 text-surface-500 hover:text-surface-300"
                  >
                    {showOpenAiKey ? <EyeOff size={16} /> : <Eye size={16} />}
                  </button>
                </div>
                {isFieldSet("openai_api_key") && (
                  <button
                    onClick={() => handleResetField("openai_api_key")}
                    className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-brand-300 shrink-0 mt-0.5"
                    title="Reset to default"
                  >
                    <X size={14} />
                  </button>
                )}
              </div>
            </div>

            {/* anthropic_api_key */}
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1">
                Anthropic API Key
                
              </label>
              <div className="flex gap-2 items-start">
                <div className="relative flex-1">
                  <input
                    className="input-base pr-10 w-full"
                    type={showAnthropicKey ? "text" : "password"}
                    placeholder="sk-ant-..."
                    value={form.anthropic_api_key}
                    onChange={(e) => updateField("anthropic_api_key", e.target.value)}
                  />
                  <button
                    type="button"
                    onClick={() => setShowAnthropicKey((prev) => !prev)}
                    className="absolute right-2.5 top-1/2 -translate-y-1/2 text-surface-500 hover:text-surface-300"
                  >
                    {showAnthropicKey ? <EyeOff size={16} /> : <Eye size={16} />}
                  </button>
                </div>
                {isFieldSet("anthropic_api_key") && (
                  <button
                    onClick={() => handleResetField("anthropic_api_key")}
                    className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-brand-300 shrink-0 mt-0.5"
                    title="Reset to default"
                  >
                    <X size={14} />
                  </button>
                )}
              </div>
            </div>

            {/* openrouter_api_key */}
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1">
                OpenRouter API Key
                
              </label>
              <div className="flex gap-2 items-start">
                <div className="relative flex-1">
                  <input
                    className="input-base pr-10 w-full"
                    type={showOpenRouterKey ? "text" : "password"}
                    placeholder="sk-or-..."
                    value={form.openrouter_api_key}
                    onChange={(e) => updateField("openrouter_api_key", e.target.value)}
                  />
                  <button
                    type="button"
                    onClick={() => setShowOpenRouterKey((prev) => !prev)}
                    className="absolute right-2.5 top-1/2 -translate-y-1/2 text-surface-500 hover:text-surface-300"
                  >
                    {showOpenRouterKey ? <EyeOff size={16} /> : <Eye size={16} />}
                  </button>
                </div>
                {isFieldSet("openrouter_api_key") && (
                  <button
                    onClick={() => handleResetField("openrouter_api_key")}
                    className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-brand-300 shrink-0 mt-0.5"
                    title="Reset to default"
                  >
                    <X size={14} />
                  </button>
                )}
              </div>
            </div>
          </div>
        )}
      </div>

      {/* ── Ollama Settings Card ──────────────────────────────────────────────── */}
      <div className="card-base p-6">
        <div className="flex items-center gap-3 mb-6">
          <div className="flex h-10 w-10 items-center justify-center rounded-full bg-surface-700">
            <Brain size={20} className="text-surface-300" />
          </div>
          <div>
            <h2 className="text-base font-semibold">Ollama Settings</h2>
            <p className="text-xs text-surface-400">Local LLM provider configuration</p>
          </div>
        </div>

        {!loading && (
          <div className="space-y-4 max-w-md">
            {/* ollama_base_url */}
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1">
                Base URL
                
              </label>
              <div className="flex gap-2 items-start">
                <input
                  className="input-base flex-1"
                  type="url"
                  placeholder="http://localhost:11434"
                  value={form.ollama_base_url}
                  onChange={(e) => updateField("ollama_base_url", e.target.value)}
                />
                {isFieldSet("ollama_base_url") && (
                  <button
                    onClick={() => handleResetField("ollama_base_url")}
                    className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-brand-300 shrink-0 mt-0.5"
                    title="Reset to default"
                  >
                    <X size={14} />
                  </button>
                )}
              </div>
            </div>

          </div>
        )}
      </div>

      {/* ── Azure Settings Card ────────────────────────────────────────────────── */}
      <div className="card-base p-6">
        <div className="flex items-center gap-3 mb-6">
          <div className="flex h-10 w-10 items-center justify-center rounded-full bg-surface-700">
            <Brain size={20} className="text-surface-300" />
          </div>
          <div>
            <h2 className="text-base font-semibold">Azure OpenAI Settings</h2>
            <p className="text-xs text-surface-400">Azure OpenAI endpoint configuration</p>
          </div>
        </div>

        {!loading && (
          <div className="space-y-4 max-w-md">
            {/* azure_openai_endpoint */}
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1">
                Endpoint URL
                
              </label>
              <div className="flex gap-2 items-start">
                <input
                  className="input-base flex-1"
                  type="url"
                  placeholder="https://my-resource.openai.azure.com"
                  value={form.azure_openai_endpoint}
                  onChange={(e) => updateField("azure_openai_endpoint", e.target.value)}
                />
                {isFieldSet("azure_openai_endpoint") && (
                  <button
                    onClick={() => handleResetField("azure_openai_endpoint")}
                    className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-brand-300 shrink-0 mt-0.5"
                    title="Reset to default"
                  >
                    <X size={14} />
                  </button>
                )}
              </div>
            </div>

            {/* azure_openai_key */}
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1">
                API Key
                
              </label>
              <div className="flex gap-2 items-start">
                <div className="relative flex-1">
                  <input
                    className="input-base pr-10 w-full"
                    type={showAzureKey ? "text" : "password"}
                    placeholder="Azure API key"
                    value={form.azure_openai_key}
                    onChange={(e) => updateField("azure_openai_key", e.target.value)}
                  />
                  <button
                    type="button"
                    onClick={() => setShowAzureKey((prev) => !prev)}
                    className="absolute right-2.5 top-1/2 -translate-y-1/2 text-surface-500 hover:text-surface-300"
                  >
                    {showAzureKey ? <EyeOff size={16} /> : <Eye size={16} />}
                  </button>
                </div>
                {isFieldSet("azure_openai_key") && (
                  <button
                    onClick={() => handleResetField("azure_openai_key")}
                    className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-brand-300 shrink-0 mt-0.5"
                    title="Reset to default"
                  >
                    <X size={14} />
                  </button>
                )}
              </div>
            </div>
          </div>
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
