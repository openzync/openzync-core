"use client";

import { useEffect, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import { Loader2, Eye, Brain, AudioWaveform, GitBranch, Settings2, Save, CheckCircle, AlertCircle, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { API_BASE } from "@/lib/api-client";
import { SecretInput } from "@/components/ui/secret-input";
import { Button } from "@/components/ui/button";

// ─── Types ─────────────────────────────────────────────────────────────────────

interface UpdateOrgConfigRequest {
  llm_backend?: string | null;
  llm_model?: string | null;
  llm_temperature?: number | null;
  llm_max_tokens?: number | null;
  openai_api_key?: string | null;
  anthropic_api_key?: string | null;
  openrouter_api_key?: string | null;
  ollama_base_url?: string | null;
  azure_openai_endpoint?: string | null;
  azure_openai_key?: string | null;
  embedding_backend?: string | null;
  embedding_model?: string | null;
  embedding_dim?: number | null;
  embedding_api_key?: string | null;
  embedding_provider?: string | null;
  graph_backend?: string | null;
  graph_search_type?: string | null;
  graph_max_traversal_depth?: number | null;
  surrealdb_url?: string | null;
  surrealdb_user?: string | null;
  surrealdb_pass?: string | null;
  surrealdb_namespace?: string | null;
  surrealdb_database?: string | null;
  context_cache_ttl?: number | null;
  audit_log_response_body?: boolean | null;
}

interface ToastState {
  visible: boolean;
  message: string;
  type: "success" | "error";
}

type LlmBackend = "openai" | "anthropic" | "ollama" | "openai_like" | "openrouter" | "azure";
type EmbeddingBackend = "openai" | "ollama" | "huggingface" | "sentence_transformers";
type GraphBackend = "postgres" | "surrealdb" | "none";
type GraphSearchType = "hybrid" | "bm25" | "vector";

// ─── Constants ─────────────────────────────────────────────────────────────────

const TOAST_DURATION = 4000;

const LLM_BACKEND_OPTIONS: { value: LlmBackend; label: string }[] = [
  { value: "openai", label: "OpenAI" },
  { value: "anthropic", label: "Anthropic" },
  { value: "openrouter", label: "OpenRouter" },
  { value: "azure", label: "Azure OpenAI" },
  { value: "ollama", label: "Ollama" },
  { value: "openai_like", label: "OpenAI-compatible" },
];

const EMBEDDING_BACKEND_OPTIONS: { value: EmbeddingBackend; label: string }[] = [
  { value: "openai", label: "OpenAI" },
  { value: "ollama", label: "Ollama" },
  { value: "huggingface", label: "Hugging Face" },
  { value: "sentence_transformers", label: "Sentence Transformers" },
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

// ─── Helpers ───────────────────────────────────────────────────────────────────

function authHeaders(): Record<string, string> {
  const token = sessionStorage.getItem("mg_access_token");
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  return headers;
}

// ─── Toast Component ───────────────────────────────────────────────────────────

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

// ─── Section Header ────────────────────────────────────────────────────────────

function SectionHeader({
  icon,
  title,
  description,
}: {
  icon: React.ReactNode;
  title: string;
  description: string;
}) {
  return (
    <div className="flex items-center gap-3 mb-6">
      <div className="flex h-10 w-10 items-center justify-center rounded-full bg-brand-500/10">
        <div className="text-brand-300">{icon}</div>
      </div>
      <div>
        <h2 className="text-base font-semibold">{title}</h2>
        <p className="text-xs text-surface-400">{description}</p>
      </div>
    </div>
  );
}

// ─── Empty String Input (shows required badge) ────────────────────────────────

// ─── Main Onboarding Page ──────────────────────────────────────────────────────

export default function OnboardingPage() {
  const router = useRouter();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<ToastState>({ visible: false, message: "", type: "success" });

  // Password visibility toggles
  const [showOpenAiKey, setShowOpenAiKey] = useState(false);
  const [showAnthropicKey, setShowAnthropicKey] = useState(false);
  const [showOpenRouterKey, setShowOpenRouterKey] = useState(false);
  const [showAzureKey, setShowAzureKey] = useState(false);
  const [showEmbeddingKey, setShowEmbeddingKey] = useState(false);
  const [showSurrealDbPass, setShowSurrealDbPass] = useState(false);

  // ── Form state ─────────────────────────────────────────────────────────────

  const [form, setForm] = useState<UpdateOrgConfigRequest>({});

  const updateField = useCallback(<K extends keyof UpdateOrgConfigRequest>(
    field: K,
    value: UpdateOrgConfigRequest[K],
  ) => {
    setForm((prev) => ({ ...prev, [field]: value }));
  }, []);

  const showToast = useCallback((message: string, type: "success" | "error") => {
    setToast({ visible: true, message, type });
  }, []);

  const dismissToast = useCallback(() => {
    setToast((prev) => ({ ...prev, visible: false }));
  }, []);

  // ── Fetch defaults ─────────────────────────────────────────────────────────

  useEffect(() => {
    async function loadDefaults() {
      setLoading(true);
      setError(null);
      try {
        // Redirect if no JWT (user came here directly without signing up)
        const token = sessionStorage.getItem("mg_access_token");
        if (!token) {
          router.replace("/signup");
          return;
        }

        // Fetch onboarding defaults (no auth required)
        const res = await fetch(`${API_BASE}/admin/org/config/defaults`);
        if (!res.ok) throw new Error("Failed to load default configuration");
        const data: UpdateOrgConfigRequest = await res.json();

        // Check if the org already has stored config — if so, redirect to dashboard
        const configRes = await fetch(`${API_BASE}/admin/org/config`, { headers: authHeaders() });
        if (configRes.ok) {
          const configData = await configRes.json();
          const stored = configData.stored as Record<string, unknown>;
          const hasAnyStored = Object.values(stored).some((v) => v !== null && v !== undefined);
          if (hasAnyStored) {
            router.replace("/overview");
            return;
          }
        }

        setForm(data);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load configuration defaults");
      } finally {
        setLoading(false);
      }
    }
    loadDefaults();
  }, [router]);

  // ── Save ───────────────────────────────────────────────────────────────────

  async function handleSave() {
    setSaving(true);
    setError(null);

    try {
      const res = await fetch(`${API_BASE}/admin/org/config`, {
        method: "PUT",
        headers: authHeaders(),
        body: JSON.stringify(form),
      });

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail ?? "Failed to save configuration");
      }

      showToast("Configuration saved successfully", "success");

      // Short delay to show the toast, then redirect
      setTimeout(() => {
        router.replace("/overview");
      }, 800);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to save configuration";
      setError(message);
      showToast(message, "error");
    } finally {
      setSaving(false);
    }
  }

  // ── Render ─────────────────────────────────────────────────────────────────

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-surface-950">
        <div className="flex flex-col items-center gap-4">
          <Loader2 size={32} className="animate-spin text-brand-500" />
          <p className="text-sm text-surface-400">Loading configuration defaults...</p>
        </div>
      </div>
    );
  }

  const anyApiKeyEmpty = !form.openai_api_key && !form.anthropic_api_key && !form.openrouter_api_key && !form.azure_openai_key;

  return (
    <div className="min-h-screen bg-surface-950">
      <div className="mx-auto max-w-3xl px-4 py-10">
        {/* ── Header ──────────────────────────────────────────────────────────── */}
        <div className="text-center mb-10">
          <div className="flex items-center justify-center gap-2 mb-4">
            <span className="text-brand-500 font-bold text-3xl">O</span>
            <span className="text-2xl font-bold text-text-primary">OpenZep</span>
          </div>
          <h1 className="text-2xl font-bold text-text-primary">Complete Your Setup</h1>
          <p className="text-sm text-surface-400 mt-1 max-w-md mx-auto">
            Configure your organization&apos;s LLM, embeddings, graph, and behaviour settings.
            Secrets like API keys must be filled in before you can use the platform.
          </p>
        </div>

        {/* ── Global error ─────────────────────────────────────────────────────── */}
        {error && (
          <div className="mb-6 rounded-md bg-error/10 border border-error/30 px-4 py-3 text-sm text-error flex items-center gap-2">
            <AlertCircle size={14} />
            {error}
          </div>
        )}

        {/* ── API Keys Notice ──────────────────────────────────────────────────── */}
        {anyApiKeyEmpty && (
          <div className="mb-6 rounded-md bg-warning/10 border border-warning/30 px-4 py-3 text-sm text-warning flex items-center gap-2">
            <AlertCircle size={14} />
            At least one LLM API key is required to use the platform. Fill in the key for your chosen provider below.
          </div>
        )}

        {/* ── LLM Section ──────────────────────────────────────────────────────── */}
        <div className="card-base p-6 mb-6">
          <SectionHeader icon={<Brain size={20} />} title="LLM Configuration" description="Primary language model provider" />

          <div className="space-y-4 max-w-md">
            {/* llm_backend */}
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1.5">Backend Provider</label>
              <select
                className="input-base w-full"
                value={form.llm_backend ?? "openai"}
                onChange={(e) => updateField("llm_backend", e.target.value)}
              >
                {LLM_BACKEND_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>{opt.label}</option>
                ))}
              </select>
            </div>

            {/* llm_model */}
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1.5">Model</label>
              <input
                className="input-base w-full"
                placeholder="gpt-4o-mini, claude-sonnet-4, ..."
                value={form.llm_model ?? ""}
                onChange={(e) => updateField("llm_model", e.target.value)}
              />
            </div>

            <div className="flex gap-4">
              {/* llm_temperature */}
              <div className="flex-1">
                <label className="block text-sm font-medium text-surface-300 mb-1.5">Temperature</label>
                <input
                  className="input-base w-full"
                  type="number"
                  step="0.1"
                  min="0"
                  max="2"
                  value={form.llm_temperature ?? 0}
                  onChange={(e) => updateField("llm_temperature", parseFloat(e.target.value) || 0)}
                />
              </div>

              {/* llm_max_tokens */}
              <div className="flex-1">
                <label className="block text-sm font-medium text-surface-300 mb-1.5">Max Tokens</label>
                <input
                  className="input-base w-full"
                  type="number"
                  min="1"
                  value={form.llm_max_tokens ?? 4096}
                  onChange={(e) => updateField("llm_max_tokens", parseInt(e.target.value) || 0)}
                />
              </div>
            </div>
          </div>
        </div>

        {/* ── API Keys Section ─────────────────────────────────────────────────── */}
        <div className="card-base p-6 mb-6">
          <SectionHeader
            icon={<Eye size={20} />}
            title="API Keys"
            description="Credentials for LLM providers — fill in the key for your chosen provider"
          />

          <div className="space-y-4 max-w-md">
            <SecretInput
              label="OpenAI API Key"
              value={form.openai_api_key ?? ""}
              onChange={(v) => updateField("openai_api_key", v)}
              placeholder="sk-..."
              visible={showOpenAiKey}
              onToggleVisibility={() => setShowOpenAiKey((prev) => !prev)}
            />
            <SecretInput
              label="Anthropic API Key"
              value={form.anthropic_api_key ?? ""}
              onChange={(v) => updateField("anthropic_api_key", v)}
              placeholder="sk-ant-..."
              visible={showAnthropicKey}
              onToggleVisibility={() => setShowAnthropicKey((prev) => !prev)}
            />
            <SecretInput
              label="OpenRouter API Key"
              value={form.openrouter_api_key ?? ""}
              onChange={(v) => updateField("openrouter_api_key", v)}
              placeholder="sk-or-..."
              visible={showOpenRouterKey}
              onToggleVisibility={() => setShowOpenRouterKey((prev) => !prev)}
            />
            <SecretInput
              label="Azure OpenAI API Key"
              value={form.azure_openai_key ?? ""}
              onChange={(v) => updateField("azure_openai_key", v)}
              placeholder="Azure API key"
              visible={showAzureKey}
              onToggleVisibility={() => setShowAzureKey((prev) => !prev)}
            />
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1.5">Azure OpenAI Endpoint</label>
              <input
                className="input-base w-full"
                type="url"
                placeholder="https://my-resource.openai.azure.com"
                value={form.azure_openai_endpoint ?? ""}
                onChange={(e) => updateField("azure_openai_endpoint", e.target.value)}
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1.5">Ollama Base URL</label>
              <input
                className="input-base w-full"
                type="url"
                placeholder="http://localhost:11434"
                value={form.ollama_base_url ?? ""}
                onChange={(e) => updateField("ollama_base_url", e.target.value)}
              />
            </div>
          </div>
        </div>

        {/* ── Embeddings Section ───────────────────────────────────────────────── */}
        <div className="card-base p-6 mb-6">
          <SectionHeader icon={<AudioWaveform size={20} />} title="Embeddings" description="Vector embedding model configuration" />

          <div className="space-y-4 max-w-md">
            {/* embedding_backend */}
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1.5">Backend Provider</label>
              <select
                className="input-base w-full"
                value={form.embedding_backend ?? "openai"}
                onChange={(e) => updateField("embedding_backend", e.target.value)}
              >
                {EMBEDDING_BACKEND_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>{opt.label}</option>
                ))}
              </select>
            </div>

            {/* embedding_model */}
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1.5">Model</label>
              <input
                className="input-base w-full"
                placeholder="text-embedding-3-small, ..."
                value={form.embedding_model ?? ""}
                onChange={(e) => updateField("embedding_model", e.target.value)}
              />
            </div>

            <div className="flex gap-4">
              {/* embedding_dim */}
              <div className="flex-1">
                <label className="block text-sm font-medium text-surface-300 mb-1.5">Embedding Dimensions</label>
                <input
                  className="input-base w-full"
                  type="number"
                  min="64"
                  max="4096"
                  value={form.embedding_dim ?? 1536}
                  onChange={(e) => updateField("embedding_dim", parseInt(e.target.value) || 0)}
                />
              </div>

              {/* embedding_provider */}
              <div className="flex-1">
                <label className="block text-sm font-medium text-surface-300 mb-1.5">Provider Name</label>
                <input
                  className="input-base w-full"
                  placeholder="openai, azure, ..."
                  value={form.embedding_provider ?? ""}
                  onChange={(e) => updateField("embedding_provider", e.target.value)}
                />
              </div>
            </div>

            {/* embedding_api_key */}
            <SecretInput
              label="Embedding API Key"
              value={form.embedding_api_key ?? ""}
              onChange={(v) => updateField("embedding_api_key", v)}
              placeholder="Embedding provider API key"
              visible={showEmbeddingKey}
              onToggleVisibility={() => setShowEmbeddingKey((prev) => !prev)}
            />
          </div>
        </div>

        {/* ── Graph Section ────────────────────────────────────────────────────── */}
        <div className="card-base p-6 mb-6">
          <SectionHeader icon={<GitBranch size={20} />} title="Knowledge Graph" description="Graph backend, search, and traversal settings" />

          <div className="space-y-4 max-w-md">
            {/* graph_backend */}
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1.5">Graph Backend</label>
              <select
                className="input-base w-full"
                value={form.graph_backend ?? "postgres"}
                onChange={(e) => updateField("graph_backend", e.target.value)}
              >
                {GRAPH_BACKEND_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>{opt.label}</option>
                ))}
              </select>
            </div>

            {/* graph_search_type */}
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1.5">Search Type</label>
              <select
                className="input-base w-full"
                value={form.graph_search_type ?? "hybrid"}
                onChange={(e) => updateField("graph_search_type", e.target.value)}
              >
                {SEARCH_TYPE_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>{opt.label}</option>
                ))}
              </select>
            </div>

            {/* SurrealDB connection fields — conditionally shown */}
            {(form.graph_backend === "surrealdb") && (
              <div className="space-y-4">
                <div>
                  <label className="block text-sm font-medium text-surface-300 mb-1.5">SurrealDB URL</label>
                  <input
                    className="input-base w-full"
                    type="url"
                    placeholder="ws://surrealdb:8000/rpc"
                    value={form.surrealdb_url ?? ""}
                    onChange={(e) => updateField("surrealdb_url", e.target.value)}
                  />
                  <p className="text-xs text-surface-500 mt-1">Required when using SurrealDB backend</p>
                </div>
                <div>
                  <label className="block text-sm font-medium text-surface-300 mb-1.5">SurrealDB Username</label>
                  <input
                    className="input-base w-full"
                    type="text"
                    placeholder="SurrealDB username"
                    value={form.surrealdb_user ?? ""}
                    onChange={(e) => updateField("surrealdb_user", e.target.value)}
                  />
                </div>
                <SecretInput
                  label="SurrealDB Password"
                  value={form.surrealdb_pass ?? ""}
                  onChange={(v) => updateField("surrealdb_pass", v)}
                  placeholder="SurrealDB password"
                  visible={showSurrealDbPass}
                  onToggleVisibility={() => setShowSurrealDbPass((prev) => !prev)}
                />
                <div>
                  <label className="block text-sm font-medium text-surface-300 mb-1.5">SurrealDB Namespace</label>
                  <input
                    className="input-base w-full"
                    type="text"
                    placeholder="SurrealDB namespace"
                    value={form.surrealdb_namespace ?? ""}
                    onChange={(e) => updateField("surrealdb_namespace", e.target.value)}
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-surface-300 mb-1.5">SurrealDB Database</label>
                  <input
                    className="input-base w-full"
                    type="text"
                    placeholder="SurrealDB database"
                    value={form.surrealdb_database ?? ""}
                    onChange={(e) => updateField("surrealdb_database", e.target.value)}
                  />
                </div>
              </div>
            )}

            {/* graph_max_traversal_depth */}
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1.5">Max Traversal Depth</label>
              <input
                className="input-base w-full"
                type="number"
                min="1"
                max="10"
                value={form.graph_max_traversal_depth ?? 2}
                onChange={(e) => updateField("graph_max_traversal_depth", parseInt(e.target.value) || 1)}
              />
              <p className="text-xs text-surface-500 mt-1">How many hops the graph traversal will follow (1&ndash;10)</p>
            </div>
          </div>
        </div>

        {/* ── Behaviour Section ────────────────────────────────────────────────── */}
        <div className="card-base p-6 mb-8">
          <SectionHeader icon={<Settings2 size={20} />} title="Behaviour" description="Caching and audit behaviour" />

          <div className="space-y-4 max-w-md">
            {/* context_cache_ttl */}
            <div>
              <label className="block text-sm font-medium text-surface-300 mb-1.5">Context Cache TTL (seconds)</label>
              <input
                className="input-base w-full"
                type="number"
                min="0"
                value={form.context_cache_ttl ?? 300}
                onChange={(e) => updateField("context_cache_ttl", parseInt(e.target.value) || 0)}
              />
              <p className="text-xs text-surface-500 mt-1">
                How long context data is cached in Redis before being re-fetched (0 = no caching)
              </p>
            </div>

            {/* audit_log_response_body */}
            <div className="pt-2">
              <div className="flex items-start justify-between">
                <div>
                  <label className="block text-sm font-medium text-surface-300 mb-1">Audit Log Response Body</label>
                  <p className="text-xs text-surface-500">Include response body content in audit logs</p>
                </div>
                <div className="flex items-center gap-2 shrink-0 ml-4">
                  <label className="flex items-center gap-3 cursor-pointer">
                    <input
                      type="checkbox"
                      className="rounded border-surface-600 bg-surface-800 text-brand-500"
                      checked={form.audit_log_response_body ?? false}
                      onChange={(e) => updateField("audit_log_response_body", e.target.checked)}
                    />
                    <span className="text-sm text-surface-300">
                      {form.audit_log_response_body ? "Enabled" : "Disabled"}
                    </span>
                  </label>
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* ── Actions ──────────────────────────────────────────────────────────── */}
        <div className="flex items-center justify-center gap-4 pb-10">
          <Button
            variant="primary"
            size="sm"
            onClick={handleSave}
            disabled={saving}
          >
            {saving ? (
              <Loader2 size={16} className="animate-spin" />
            ) : (
              <Save size={16} />
            )}
            {saving ? "Saving..." : "Save & Continue"}
          </Button>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => router.replace("/overview")}
            disabled={saving}
          >
            Skip for now
          </Button>
        </div>
      </div>

      {/* ── Toast ─────────────────────────────────────────────────────────────── */}
      <Toast toast={toast} onDismiss={dismissToast} />
    </div>
  );
}
