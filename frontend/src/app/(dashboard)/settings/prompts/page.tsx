"use client";
import { RequireAuth } from "../../require-auth";

import { useEffect, useState, useCallback } from "react";
import {
  Download,
  FileText,
  Star,
  Plus,
  X,
  Trash2,
  CheckCircle,
  AlertCircle,
  History,
  RotateCcw,
  Eye,
  Edit3,
  Save,
} from "lucide-react";
import { cn } from "@/lib/utils";

// ─── Types ─────────────────────────────────────────────────────────────────────

interface PromptTemplateSummary {
  name: string;
  version: number;
  is_customised: boolean;
  description: string | null;
  type: string | null;
  is_default_for_type: boolean;
  updated_at: string;
}

interface PromptTemplateDetail {
  name: string;
  version: number;
  template_text: string;
  description: string | null;
  type: string | null;
  is_active: boolean;
  is_default_for_type?: boolean;
  is_customised?: boolean;
  is_system_default?: boolean;
}

interface PromptTemplateVersions {
  name: string;
  current_version: number;
  versions: PromptTemplateDetail[];
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

function formatDate(dateStr: string): string {
  const d = new Date(dateStr);
  return d.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function timeAgo(dateStr: string): string {
  const seconds = Math.floor((Date.now() - new Date(dateStr).getTime()) / 1000);
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const mins = Math.floor(seconds / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

function templateDisplayName(name: string): string {
  // Convert snake_case names like "extraction_summary" to "Extraction Summary"
  return name
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
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

// ─── Edit Dialog ───────────────────────────────────────────────────────────────

interface EditDialogProps {
  template: PromptTemplateDetail;
  onClose: () => void;
  onSaved: () => Promise<void>;
  onReset: () => Promise<void>;
  onShowHistory: () => void;
  showToast: (message: string, type: "success" | "error") => void;
}

function EditDialog({ template, onClose, onSaved, onReset, onShowHistory, showToast }: EditDialogProps) {
  const [templateText, setTemplateText] = useState(template.template_text);
  const [description, setDescription] = useState(template.description ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showResetConfirm, setShowResetConfirm] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [settingDefault, setSettingDefault] = useState(false);

  // Reset form when template changes
  useEffect(() => {
    setTemplateText(template.template_text);
    setDescription(template.description ?? "");
    setError(null);
    setShowResetConfirm(false);
  }, [template]);

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!templateText.trim()) {
      setError("Template text cannot be empty");
      return;
    }

    setSaving(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/admin/org/prompts/${template.name}`, {
        method: "PUT",
        headers: authHeaders(),
        body: JSON.stringify({
          template_text: templateText,
          description: description.trim() || null,
        }),
      });

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail ?? "Failed to save template");
      }

      showToast(`Template "${templateDisplayName(template.name)}" saved as new version`, "success");
      await onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save template");
    } finally {
      setSaving(false);
    }
  };

  const handleReset = async () => {
    setResetting(true);
    try {
      const res = await fetch(`${API_BASE}/admin/org/prompts/${template.name}`, {
        method: "DELETE",
        headers: authHeaders(),
      });

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail ?? "Failed to reset template");
      }

      setShowResetConfirm(false);
      showToast(`Template "${templateDisplayName(template.name)}" reset to default`, "success");
      await onReset();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to reset template");
    } finally {
      setResetting(false);
    }
  };

  const handleSetDefault = async () => {
    if (!template.type || template.is_default_for_type) return;
    setSettingDefault(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/admin/org/prompts/${template.name}/set-default`, {
        method: "POST",
        headers: authHeaders(),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail ?? "Failed to set as default");
      }
      showToast(`"${templateDisplayName(template.name)}" is now the default for its type`, "success");
      await onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to set as default");
    } finally {
      setSettingDefault(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 animate-fade-in" onClick={onClose}>
      <div
        className="card-base w-full max-w-3xl p-6 shadow-xl shadow-black/40 animate-slide-up max-h-[90vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-md bg-brand-500/10 shrink-0">
              <FileText size={16} className="text-brand-300" />
            </div>
            <div>
              <h2 className="text-lg font-semibold">{templateDisplayName(template.name)}</h2>
              <p className="text-xs text-surface-400">Version {template.version}</p>
            </div>
          </div>
          <button onClick={onClose} className="btn-ghost p-1 rounded-md text-surface-400 hover:text-white">
            <X size={18} />
          </button>
        </div>

        {/* Warning banner */}
        <div className="flex items-start gap-2 rounded-lg border border-amber-500/20 bg-amber-500/5 px-3 py-2 mb-4">
          <AlertCircle size={13} className="text-amber-400 mt-0.5 shrink-0" />
          <p className="text-xs text-amber-200/80 leading-relaxed">
            Custom prompts override the system default. Incorrect Jinja2 syntax may cause extraction failures.
          </p>
        </div>

        <form onSubmit={handleSave} className="space-y-4">
          {/* Description */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">
              Description <span className="text-surface-500">(optional)</span>
            </label>
            <input
              className="input-base"
              placeholder="Describe what this template does"
              value={description}
              onChange={(e) => { setDescription(e.target.value); if (error) setError(null); }}
              disabled={saving}
            />
          </div>

          {/* Template text */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">
              Template <span className="text-error">*</span>
            </label>
            <textarea
              className={cn(
                "w-full rounded-lg border border-surface-700 bg-surface-950 p-4 text-sm font-mono leading-relaxed",
                "text-surface-100 placeholder-surface-500 outline-none resize-y min-h-[300px]",
                "transition-all duration-150 focus:border-accent-300 focus:shadow-[0_0_0_2px_rgba(143,175,217,0.2)]",
              )}
              placeholder="{% raw %}{{ Enter your Jinja2 template here }}{% endraw %}"
              value={templateText}
              onChange={(e) => { setTemplateText(e.target.value); if (error) setError(null); }}
              disabled={saving}
              spellCheck={false}
            />
            <p className="text-xs text-surface-500 mt-1">
              Jinja2 template syntax. Use {"{{ variables }}"} and {"{% tags %}"} for dynamic content.
            </p>
          </div>

          {error && (
            <div className="rounded-md bg-error/10 border border-error/30 px-3 py-2 text-sm text-error flex items-center gap-2">
              <AlertCircle size={14} />
              {error}
            </div>
          )}

          {/* Action bar */}
          <div className="flex items-center justify-between pt-2 border-t border-surface-800">
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={onShowHistory}
                className="btn-ghost text-sm"
              >
                <History size={14} />
                Version History
              </button>
              {template.type && !template.is_default_for_type && (
                <button
                  type="button"
                  onClick={handleSetDefault}
                  disabled={settingDefault}
                  className="btn-ghost text-xs text-amber-400 hover:text-amber-300"
                >
                  {settingDefault ? (
                    <Spinner className="text-amber-400" />
                  ) : (
                    <><Star size={12} /> Set as Default</>
                  )}
                </button>
              )}
            </div>

            <div className="flex items-center gap-3">
              {template.is_customised && (
                <>
                  {showResetConfirm ? (
                    <div className="flex items-center gap-2">
                      <span className="text-xs text-surface-400">Reset to default?</span>
                      <button
                        type="button"
                        onClick={handleReset}
                        disabled={resetting}
                        className="btn-danger text-xs min-w-[80px] justify-center"
                      >
                        {resetting ? (
                          <span className="flex items-center gap-1"><Spinner /> Resetting...</span>
                        ) : (
                          <span className="flex items-center gap-1"><Trash2 size={12} /> Confirm</span>
                        )}
                      </button>
                      <button
                        type="button"
                        onClick={() => setShowResetConfirm(false)}
                        className="btn-ghost text-xs"
                        disabled={resetting}
                      >
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <button
                      type="button"
                      onClick={() => setShowResetConfirm(true)}
                      className="btn-ghost text-xs text-surface-400 hover:text-error"
                    >
                      <RotateCcw size={12} />
                      Reset to Default
                    </button>
                  )}
                </>
              )}

              <button type="button" onClick={onClose} className="btn-secondary text-sm" disabled={saving}>
                Cancel
              </button>
              <button
                type="submit"
                disabled={saving || (!templateText.trim() && !error)}
                className="btn-primary text-sm min-w-[120px] justify-center"
              >
                {saving ? (
                  <span className="flex items-center gap-2">
                    <Spinner /> Saving...
                  </span>
                ) : (
                  <span className="flex items-center gap-2">
                    <Save size={14} /> Save as New Version
                  </span>
                )}
              </button>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}

// ─── Version History Dialog ────────────────────────────────────────────────────

interface VersionHistoryDialogProps {
  templateName: string;
  onClose: () => void;
  onRollback: (version: number) => Promise<void>;
  showToast: (message: string, type: "success" | "error") => void;
}

function VersionHistoryDialog({ templateName, onClose, onRollback, showToast }: VersionHistoryDialogProps) {
  const [versionsData, setVersionsData] = useState<PromptTemplateVersions | null>(null);
  const [loading, setLoading] = useState(true);
  const [selectedVersion, setSelectedVersion] = useState<PromptTemplateDetail | null>(null);
  const [rollingBack, setRollingBack] = useState(false);
  const [promoting, setPromoting] = useState(false);
  const [promoteConfirm, setPromoteConfirm] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fetchVersions = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/admin/org/prompts/${templateName}/versions`, {
        headers: authHeaders(),
      });
      if (!res.ok) throw new Error("Failed to load version history");
      const data: PromptTemplateVersions = await res.json();
      setVersionsData(data);
      // Auto-select the currently active version
      const active = data.versions.find((v) => v.is_active);
      if (active) setSelectedVersion(active);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load version history");
    } finally {
      setLoading(false);
    }
  }, [templateName]);

  useEffect(() => {
    fetchVersions();
  }, [fetchVersions]);

  const handleRollback = async (version: number) => {
    setRollingBack(true);
    try {
      const res = await fetch(
        `${API_BASE}/admin/org/prompts/${templateName}/rollback/${version}`,
        { method: "POST", headers: authHeaders() },
      );
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail ?? "Failed to rollback template");
      }
      showToast(
        `Template "${templateDisplayName(templateName)}" rolled back to version ${version}`,
        "success",
      );
      await fetchVersions();
      onRollback(version);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to rollback template");
    } finally {
      setRollingBack(false);
    }
  };

  const handlePromote = async (version: number) => {
    setPromoting(true);
    setPromoteConfirm(null);
    try {
      const res = await fetch(
        `${API_BASE}/admin/org/prompts/${templateName}/promote/${version}`,
        { method: "POST", headers: authHeaders() },
      );
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail ?? "Failed to promote template");
      }
      showToast(
        `Version ${version} of "${templateDisplayName(templateName)}" is now the system default`,
        "success",
      );
      await fetchVersions();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to promote template");
    } finally {
      setPromoting(false);
    }
  };

  const isActiveVersion = (version: number) =>
    versionsData?.current_version === version;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 animate-fade-in" onClick={onClose}>
      <div
        className="card-base w-full max-w-4xl p-6 shadow-xl shadow-black/40 animate-slide-up max-h-[90vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4 shrink-0">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-md bg-surface-800 shrink-0">
              <History size={16} className="text-surface-300" />
            </div>
            <div>
              <h2 className="text-lg font-semibold">Version History</h2>
              <p className="text-xs text-surface-400">{templateDisplayName(templateName)}</p>
            </div>
          </div>
          <button onClick={onClose} className="btn-ghost p-1 rounded-md text-surface-400 hover:text-white">
            <X size={18} />
          </button>
        </div>

        {error && (
          <div className="rounded-md bg-error/10 border border-error/30 px-3 py-2 text-sm text-error flex items-center gap-2 mb-4 shrink-0">
            <AlertCircle size={14} />
            {error}
          </div>
        )}

        <div className="flex gap-4 flex-1 min-h-0">
          {/* Version list — left panel */}
          <div className="w-56 shrink-0 overflow-y-auto border border-surface-800 rounded-md">
            {loading ? (
              <div className="flex items-center justify-center py-12">
                <Spinner className="text-surface-400" />
              </div>
            ) : !versionsData || versionsData.versions.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-12 text-surface-500">
                <History size={24} className="mb-2 opacity-40" />
                <p className="text-xs">No versions found</p>
              </div>
            ) : (
              <div className="divide-y divide-surface-800">
                {versionsData.versions.map((v) => {
                  const isActive = isActiveVersion(v.version);
                  return (
                    <button
                      key={v.version}
                      onClick={() => setSelectedVersion(v)}
                      className={cn(
                        "w-full text-left px-3 py-2.5 transition-colors hover:bg-surface-800/50",
                        selectedVersion?.version === v.version && "bg-surface-800",
                      )}
                    >
                      <div className="flex items-center justify-between">
                        <span className={cn(
                          "text-sm font-medium",
                          isActive ? "text-success" : "text-surface-300",
                        )}>
                          v{v.version}
                        </span>
                        {v.is_system_default && (
                          <span className="text-[10px] font-medium text-amber-400">Default</span>
                        )}
                        {isActive && !v.is_system_default && (
                          <span className="text-[10px] font-medium text-success">Active</span>
                        )}
                      </div>
                      <p className="text-[11px] text-surface-500 mt-0.5 truncate">
                        {v.description || "No description"}
                      </p>
                    </button>
                  );
                })}
              </div>
            )}
          </div>

          {/* Template preview — right panel */}
          <div className="flex-1 flex flex-col min-w-0">
            {selectedVersion ? (
              <>
                <div className="flex items-center justify-between mb-2 shrink-0">
                  <div className="flex items-center gap-2">
                    <Eye size={14} className="text-surface-400" />
                    <span className="text-sm font-medium text-surface-200">
                      Version {selectedVersion.version}
                    </span>
                    {isActiveVersion(selectedVersion.version) && (
                      <span className="inline-flex items-center rounded-full bg-success/10 text-success px-2 py-0.5 text-[10px] font-medium">
                        Currently Active
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-2">
                    {!isActiveVersion(selectedVersion.version) && (
                      <button
                        onClick={() => handleRollback(selectedVersion.version)}
                        disabled={rollingBack}
                        className="btn-primary text-xs min-w-[100px] justify-center"
                      >
                        {rollingBack ? (
                          <span className="flex items-center gap-1"><Spinner /> Rolling back...</span>
                        ) : (
                          <span className="flex items-center gap-1">
                            <RotateCcw size={12} /> Rollback to v{selectedVersion.version}
                          </span>
                        )}
                      </button>
                    )}
                    {!selectedVersion.is_system_default ? (
                      promoteConfirm === selectedVersion.version ? (
                        <div className="flex items-center gap-2">
                          <span className="text-xs text-amber-400">Set as global default?</span>
                          <button
                            onClick={() => handlePromote(selectedVersion.version)}
                            disabled={promoting}
                            className="btn-primary text-xs bg-amber-500/20 border-amber-500/30 hover:bg-amber-500/30 min-w-[80px] justify-center"
                          >
                            {promoting ? (
                              <span className="flex items-center gap-1"><Spinner /> Promoting...</span>
                            ) : (
                              <span className="flex items-center gap-1"><Star size={12} /> Confirm</span>
                            )}
                          </button>
                          <button
                            onClick={() => setPromoteConfirm(null)}
                            className="btn-ghost text-xs text-surface-400"
                            disabled={promoting}
                          >
                            Cancel
                          </button>
                        </div>
                      ) : (
                        <button
                          onClick={() => setPromoteConfirm(selectedVersion.version)}
                          className="btn-ghost text-xs text-amber-400 hover:text-amber-300"
                        >
                          <Star size={12} /> Set as System Default
                        </button>
                      )
                    ) : (
                      <span className="inline-flex items-center gap-1 text-xs text-amber-400">
                        <Star size={12} /> System Default
                      </span>
                    )}
                  </div>
                </div>

                <div className="relative flex-1">
                  <pre className="absolute inset-0 overflow-auto rounded-lg border border-surface-700 bg-surface-950 p-4 text-xs font-mono leading-relaxed text-surface-100 whitespace-pre-wrap break-all">
                    {selectedVersion.template_text}
                  </pre>
                </div>
              </>
            ) : (
              <div className="flex flex-col items-center justify-center h-full text-surface-500">
                <Eye size={32} className="mb-2 opacity-40" />
                <p className="text-sm">Select a version to preview</p>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── Browse System Prompts Dialog ───────────────────────────────────────────────

interface SystemTemplateEntry {
  name: string;
  version: number;
  type: string | null;
  is_active: boolean;
  is_default_for_type: boolean;
  is_system_default: boolean;
  description: string | null;
}

interface SystemPromptGroup {
  type: string;
  templates: SystemTemplateEntry[];
  imported: string[];
}

interface SystemPromptGroupsResponse {
  groups: SystemPromptGroup[];
}

function BrowserDialog({ onClose, onImported, showToast }: {
  onClose: () => void;
  onImported: () => void;
  showToast: (message: string, type: "success" | "error") => void;
}) {
  const [groups, setGroups] = useState<SystemPromptGroup[]>([]);
  const [loading, setLoading] = useState(true);
  const [importing, setImporting] = useState<string | null>(null);

  const fetchSystemPrompts = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/admin/org/prompts/system`, { headers: authHeaders() });
      if (!res.ok) throw new Error("Failed to load system prompts");
      const data: SystemPromptGroupsResponse = await res.json();
      setGroups(data.groups ?? []);
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Failed to load system prompts", "error");
      onClose();
    } finally {
      setLoading(false);
    }
  }, [showToast, onClose]);

  useEffect(() => { fetchSystemPrompts(); }, [fetchSystemPrompts]);

  const handleImport = async (templateName: string) => {
    setImporting(templateName);
    try {
      const res = await fetch(`${API_BASE}/admin/org/prompts/import`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ template_name: templateName }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail ?? "Failed to import template");
      }
      showToast(`"${templateDisplayName(templateName)}" imported successfully`, "success");
      await fetchSystemPrompts();
      onImported();
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Failed to import template", "error");
    } finally {
      setImporting(null);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 animate-fade-in" onClick={onClose}>
      <div
        className="card-base w-full max-w-2xl p-6 shadow-xl shadow-black/40 animate-slide-up max-h-[85vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4 shrink-0">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-md bg-surface-800 shrink-0">
              <Download size={16} className="text-surface-300" />
            </div>
            <div>
              <h2 className="text-lg font-semibold">Browse System Prompts</h2>
              <p className="text-xs text-surface-400">Import system-default prompt templates into your organisation</p>
            </div>
          </div>
          <button onClick={onClose} className="btn-ghost p-1 rounded-md text-surface-400 hover:text-white">
            <X size={18} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto space-y-4 min-h-0">
          {loading ? (
            <div className="flex items-center justify-center py-12">
              <Spinner className="text-surface-400" />
            </div>
          ) : groups.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 text-surface-500">
              <Download size={32} className="mb-2 opacity-40" />
              <p className="text-sm">No system prompts available</p>
            </div>
          ) : (
            groups.map((group) => (
              <div key={group.type} className="border border-surface-800 rounded-md overflow-hidden">
                <div className="bg-surface-800/50 px-3 py-2 text-sm font-medium text-surface-200">
                  {templateDisplayName(group.type)}
                </div>
                <div className="divide-y divide-surface-800">
                  {group.templates.map((t) => {
                    const isImported = group.imported.includes(t.name);
                    return (
                      <div key={t.name} className="flex items-center justify-between px-3 py-2.5 hover:bg-surface-800/30">
                        <div className="flex items-center gap-2">
                          <span className="text-sm font-mono text-surface-200">{t.name}</span>
                          <span className="text-[10px] text-surface-500">v{t.version}</span>
                          {t.is_system_default && (
                            <span className="inline-flex items-center gap-0.5 rounded-full bg-amber-500/10 text-amber-400 px-1.5 py-0.5 text-[10px] font-medium">
                              <Star size={10} /> Active Default
                            </span>
                          )}
                          {!t.is_active && !t.is_system_default && (
                            <span className="text-[10px] text-surface-500">inactive</span>
                          )}
                        </div>
                        {isImported ? (
                          <span className="inline-flex items-center gap-1 text-xs text-success">
                            <CheckCircle size={12} /> Imported
                          </span>
                        ) : (
                          <button
                            onClick={() => handleImport(t.name)}
                            disabled={importing === t.name}
                            className="btn-primary text-xs min-w-[80px] justify-center"
                          >
                            {importing === t.name ? (
                              <span className="flex items-center gap-1"><Spinner /> Importing...</span>
                            ) : (
                              "Import"
                            )}
                          </button>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            ))
          )}
        </div>

        <div className="flex justify-end mt-4 shrink-0 pt-3 border-t border-surface-800">
          <button onClick={onClose} className="btn-ghost text-sm">Close</button>
        </div>
      </div>
    </div>
  );
}


// ─── Create Dialog ──────────────────────────────────────────────────────────────

const KNOWN_TYPES = [
  { value: "fact_extraction", label: "Fact Extraction" },
  { value: "entity_extraction", label: "Entity Extraction" },
  { value: "classification", label: "Classification" },
  { value: "structured_extraction", label: "Structured Extraction" },
  { value: "user_summary", label: "User Summary" },
];

interface CreateDialogProps {
  onClose: () => void;
  onCreate: () => Promise<void>;
  showToast: (message: string, type: "success" | "error") => void;
}

function CreateDialog({ onClose, onCreate, showToast }: CreateDialogProps) {
  const [name, setName] = useState("");
  const [type, setType] = useState("");
  const [description, setDescription] = useState("");
  const [templateText, setTemplateText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmedName = name.trim();
    const trimmedText = templateText.trim();
    if (!trimmedName) { setError("Template name is required"); return; }
    if (!trimmedText) { setError("Template text is required"); return; }

    setCreating(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/admin/org/prompts/${encodeURIComponent(trimmedName)}`, {
        method: "PUT",
        headers: authHeaders(),
        body: JSON.stringify({
          template_text: trimmedText,
          description: description.trim() || null,
          type: type || null,
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail ?? "Failed to create template");
      }
      showToast(`Template "${templateDisplayName(trimmedName)}" created`, "success");
      await onCreate();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create template");
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 animate-fade-in" onClick={onClose}>
      <div
        className="card-base w-full max-w-3xl p-6 shadow-xl shadow-black/40 animate-slide-up max-h-[90vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-md bg-brand-500/10 shrink-0">
              <FileText size={16} className="text-brand-300" />
            </div>
            <div>
              <h2 className="text-lg font-semibold">New Prompt Template</h2>
              <p className="text-xs text-surface-400">Create a custom prompt template for your organisation</p>
            </div>
          </div>
          <button onClick={onClose} className="btn-ghost p-1 rounded-md text-surface-400 hover:text-white">
            <X size={18} />
          </button>
        </div>

        {/* Warning banner */}
        <div className="flex items-start gap-2 rounded-lg border border-amber-500/20 bg-amber-500/5 px-3 py-2 mb-4">
          <AlertCircle size={13} className="text-amber-400 mt-0.5 shrink-0" />
          <p className="text-xs text-amber-200/80 leading-relaxed">
            Incorrect Jinja2 syntax may cause extraction failures. Use the existing templates as reference.
          </p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          {/* Name */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">
              Template Name <span className="text-error">*</span>
            </label>
            <input
              className="input-base"
              placeholder="e.g. my_custom_ner_v1"
              value={name}
              onChange={(e) => { setName(e.target.value); if (error) setError(null); }}
              autoFocus
              disabled={creating}
            />
            <p className="text-xs text-surface-500 mt-1">Unique identifier used as the template key in the system.</p>
          </div>

          {/* Type */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Type</label>
            <select
              className="input-base"
              value={type}
              onChange={(e) => setType(e.target.value)}
              disabled={creating}
            >
              <option value="">— No type —</option>
              {KNOWN_TYPES.map((t) => (
                <option key={t.value} value={t.value}>{t.label}</option>
              ))}
            </select>
            <p className="text-xs text-surface-500 mt-1">
              Groups this template with a specific extraction worker type.
            </p>
          </div>

          {/* Description */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">
              Description <span className="text-surface-500">(optional)</span>
            </label>
            <input
              className="input-base"
              placeholder="Describe what this template does"
              value={description}
              onChange={(e) => { setDescription(e.target.value); if (error) setError(null); }}
              disabled={creating}
            />
          </div>

          {/* Template text */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">
              Template <span className="text-error">*</span>
            </label>
            <textarea
              className={cn(
                "w-full rounded-lg border border-surface-700 bg-surface-950 p-4 text-sm font-mono leading-relaxed",
                "text-surface-100 placeholder-surface-500 outline-none resize-y min-h-[300px]",
                "transition-all duration-150 focus:border-accent-300 focus:shadow-[0_0_0_2px_rgba(143,175,217,0.2)]",
              )}
              placeholder="{% raw %}{{ Enter your Jinja2 template here }}{% endraw %}"
              value={templateText}
              onChange={(e) => { setTemplateText(e.target.value); if (error) setError(null); }}
              disabled={creating}
              spellCheck={false}
            />
            <p className="text-xs text-surface-500 mt-1">
              Jinja2 template syntax. Use {"{{ variables }}"} and {"{% tags %}"} for dynamic content.
            </p>
          </div>

          {error && (
            <div className="rounded-md bg-error/10 border border-error/30 px-3 py-2 text-sm text-error flex items-center gap-2">
              <AlertCircle size={14} />
              {error}
            </div>
          )}

          {/* Action bar */}
          <div className="flex items-center justify-end gap-3 pt-2 border-t border-surface-800">
            <button type="button" onClick={onClose} className="btn-secondary text-sm" disabled={creating}>
              Cancel
            </button>
            <button
              type="submit"
              disabled={creating || !name.trim() || !templateText.trim()}
              className="btn-primary text-sm min-w-[140px] justify-center"
            >
              {creating ? (
                <span className="flex items-center gap-2">
                  <Spinner /> Creating...
                </span>
              ) : (
                <span className="flex items-center gap-2">
                  <Plus size={14} /> Create Template
                </span>
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ─── Delete Dialog ──────────────────────────────────────────────────────────────

interface DeleteDialogProps {
  templateName: string;
  templateDisplay: string;
  onClose: () => void;
  onConfirm: () => Promise<void>;
}

function DeleteDialog({ templateName, templateDisplay, onClose, onConfirm }: DeleteDialogProps) {
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
            <h2 className="text-lg font-semibold">Delete Template</h2>
            <p className="text-sm text-surface-400">This action cannot be undone.</p>
          </div>
        </div>

        <p className="text-sm text-surface-300 mb-2">
          Are you sure you want to delete
        </p>
        <p className="text-sm font-medium text-white mb-5">
          &ldquo;{templateDisplay}&rdquo;
        </p>
        <p className="text-xs text-surface-500 mb-5">
          All custom versions will be removed. The organisation will fall back to the system default.
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

export default function PromptsPage() {
  const [templates, setTemplates] = useState<PromptTemplateSummary[]>([]);
  const [loading, setLoading] = useState(true);

  const [editTarget, setEditTarget] = useState<PromptTemplateDetail | null>(null);
  const [historyTarget, setHistoryTarget] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [showBrowser, setShowBrowser] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);

  const [toast, setToast] = useState<ToastState>({ visible: false, message: "", type: "success" });

  const showToast = useCallback((message: string, type: "success" | "error") => {
    setToast({ visible: true, message, type });
  }, []);

  const dismissToast = useCallback(() => {
    setToast((prev) => ({ ...prev, visible: false }));
  }, []);

  // ── Fetch templates ──────────────────────────────────────────────────────

  const fetchTemplates = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/admin/org/prompts`, { headers: authHeaders() });
      if (!res.ok) throw new Error("Failed to load prompt templates");
      const json = await res.json();
      setTemplates(json.data ?? []);
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Failed to load prompt templates", "error");
    } finally {
      setLoading(false);
    }
  }, [showToast]);

  useEffect(() => {
    fetchTemplates();
  }, [fetchTemplates]);

  // ── Fetch single template detail for editing ─────────────────────────────

  const openEditor = useCallback(async (name: string, is_customised = false) => {
    try {
      const res = await fetch(`${API_BASE}/admin/org/prompts/${name}`, {
        headers: authHeaders(),
      });
      if (!res.ok) throw new Error("Failed to load template");
      const data: PromptTemplateDetail = await res.json();
      setEditTarget({ ...data, is_customised });
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Failed to load template", "error");
    }
  }, [showToast]);

  // ── Callbacks from child dialogs ─────────────────────────────────────────

  const handleSaved = useCallback(async () => {
    await fetchTemplates();
    if (editTarget) {
      // Refresh the editor with the latest data
      await openEditor(editTarget.name, editTarget.is_customised);
    }
  }, [fetchTemplates, editTarget, openEditor]);

  const handleReset = useCallback(async () => {
    setEditTarget(null);
    await fetchTemplates();
  }, [fetchTemplates]);

  const handleRollback = useCallback(async (_version: number) => {
    setHistoryTarget(null);
    await fetchTemplates();
  }, [fetchTemplates]);

  const handleDelete = useCallback(async () => {
    if (!deleteTarget) return;
    try {
      const res = await fetch(`${API_BASE}/admin/org/prompts/${deleteTarget}`, {
        method: "DELETE",
        headers: authHeaders(),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail ?? "Failed to delete template");
      }
      showToast(`"${templateDisplayName(deleteTarget)}" reverted to system default`, "success");
      setDeleteTarget(null);
      await fetchTemplates();
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Failed to delete template", "error");
      setDeleteTarget(null);
    }
  }, [deleteTarget, showToast, fetchTemplates]);

  // ── Skeleton rows ────────────────────────────────────────────────────────

  const skeletonRows = Array.from({ length: 4 }, (_, i) => (
    <tr key={`skel-${i}`} className={i % 2 === 0 ? "bg-surface-950/50" : ""}>
      {[1, 2, 3, 4, 5].map((col) => (
        <td key={col} className="px-4 py-3">
          <div className="h-4 rounded bg-surface-800 animate-pulse" style={{ width: col === 1 ? "140px" : col === 4 ? "100px" : "80px" }} />
        </td>
      ))}
    </tr>
  ));

  // ── Empty state ──────────────────────────────────────────────────────────

  const emptyRow = (
    <tr>
      <td colSpan={5}>
        <div className="flex flex-col items-center justify-center py-16 text-surface-500">
          <FileText size={40} className="mb-3 opacity-40" />
          <p className="text-sm font-medium">No prompt templates available</p>
          <p className="text-xs mt-1">Prompt templates are loaded from the server</p>
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
          <h1 className="text-2xl font-bold tracking-tight">Prompt Templates</h1>
          <p className="text-sm text-surface-400 mt-1">
            Manage Jinja2 prompt templates for extraction workers. Organised by type with version history and rollback support.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <button onClick={() => setShowCreate(true)} className="btn-primary text-sm">
            <Plus size={16} /> New Template
          </button>
          <button onClick={() => setShowBrowser(true)} className="btn-secondary text-sm">
            <Download size={14} /> Browse System Prompts
          </button>
        </div>
      </div>

      {/* Table */}
      <div className="card-base overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-surface-800">
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Name</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Version</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Type</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Updated</th>
                <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-surface-400">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-surface-800">
              {loading ? (
                skeletonRows
              ) : templates.length === 0 ? (
                emptyRow
              ) : (
                (() => {
                  // Sort by type, then by name
                  const sorted = [...templates].sort((a, b) => {
                    const typeA = a.type || "\uffff";
                    const typeB = b.type || "\uffff";
                    if (typeA !== typeB) return typeA.localeCompare(typeB);
                    return b.version - a.version;
                  });
                  const rows: React.ReactNode[] = [];
                  let currentType: string | null = null;
                  sorted.forEach((tmpl) => {
                    if (tmpl.type !== currentType) {
                      currentType = tmpl.type;
                      rows.push(
                        <tr key={`group-${currentType ?? "untagged"}`}>
                          <td
                            colSpan={5}
                            className="px-4 py-1.5 bg-surface-800/70"
                          >
                            <span className="text-xs font-medium text-surface-300">
                              {currentType
                                ? templateDisplayName(currentType)
                                : "Other"}
                            </span>
                          </td>
                        </tr>,
                      );
                    }
                    rows.push(
                      <tr
                        key={tmpl.name}
                        className="transition-colors hover:bg-surface-800/50"
                      >
                        {/* Name */}
                        <td className="px-4 py-3">
                          <div>
                            <span className="font-medium text-white">
                              {templateDisplayName(tmpl.name)}
                            </span>
                            {tmpl.description && (
                              <p className="text-[11px] text-surface-500 mt-0.5 truncate max-w-[200px]">
                                {tmpl.description}
                              </p>
                            )}
                          </div>
                        </td>

                        {/* Version */}
                        <td className="px-4 py-3">
                          <span className="font-mono text-xs text-surface-300">
                            v{tmpl.version}
                          </span>
                        </td>

                        {/* Type badge + default indicator */}
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-2">
                            {tmpl.type ? (
                              <span className="inline-flex items-center rounded-full bg-brand-500/10 text-brand-300 px-2 py-0.5 text-[11px] font-medium">
                                {templateDisplayName(tmpl.type)}
                              </span>
                            ) : (
                              <span className="text-[11px] text-surface-500">
                                —
                              </span>
                            )}
                            {tmpl.is_default_for_type && (
                              <span className="inline-flex items-center gap-0.5 rounded-full bg-amber-500/10 text-amber-400 px-1.5 py-0.5 text-[10px] font-medium">
                                <Star size={10} /> Default
                              </span>
                            )}
                          </div>
                        </td>

                        {/* Updated */}
                        <td className="px-4 py-3">
                          <span
                            className="text-surface-400 text-xs"
                            title={formatDate(tmpl.updated_at)}
                          >
                            {timeAgo(tmpl.updated_at)}
                          </span>
                        </td>

                        {/* Actions */}
                        <td className="px-4 py-3 text-right">
                          <div className="flex items-center justify-end gap-1">
                            <button
                              onClick={() =>
                                openEditor(tmpl.name, tmpl.is_customised)
                              }
                              className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-brand-300"
                              title="Edit template"
                            >
                              <Edit3 size={14} />
                            </button>
                            <button
                              onClick={() => setHistoryTarget(tmpl.name)}
                              className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-surface-200"
                              title="View version history"
                            >
                              <History size={14} />
                            </button>
                            {tmpl.is_customised && (
                              <button
                                onClick={() => setDeleteTarget(tmpl.name)}
                                className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-error"
                                title="Delete template (revert to system default)"
                              >
                                <Trash2 size={14} />
                              </button>
                            )}
                          </div>
                        </td>
                      </tr>,
                    );
                  });
                  return rows;
                })()
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* ── Edit Dialog ──────────────────────────────────────────────────────── */}
      {editTarget && (
        <EditDialog
          template={editTarget}
          onClose={() => setEditTarget(null)}
          onSaved={handleSaved}
          onReset={handleReset}
          onShowHistory={() => {
            const name = editTarget.name;
            setEditTarget(null);
            // Small delay so the editor closes before history opens
            setTimeout(() => setHistoryTarget(name), 100);
          }}
          showToast={showToast}
        />
      )}

      {/* ── Version History Dialog ───────────────────────────────────────────── */}
      {historyTarget && (
        <VersionHistoryDialog
          templateName={historyTarget}
          onClose={() => setHistoryTarget(null)}
          onRollback={handleRollback}
          showToast={showToast}
        />
      )}

      {/* ── Create Template Dialog ────────────────────────────────────────────── */}
      {showCreate && (
        <CreateDialog
          onClose={() => setShowCreate(false)}
          onCreate={() => { setShowCreate(false); return fetchTemplates(); }}
          showToast={showToast}
        />
      )}

      {/* ── Delete Template Dialog ────────────────────────────────────────────── */}
      {deleteTarget && (
        <DeleteDialog
          templateName={deleteTarget}
          templateDisplay={templateDisplayName(deleteTarget)}
          onClose={() => setDeleteTarget(null)}
          onConfirm={handleDelete}
        />
      )}

      {/* ── Browse System Prompts Dialog ──────────────────────────────────────── */}
      {showBrowser && (
        <BrowserDialog
          onClose={() => setShowBrowser(false)}
          onImported={fetchTemplates}
          showToast={showToast}
        />
      )}

      {/* ── Toast ─────────────────────────────────────────────────────────────── */}
      <Toast toast={toast} onDismiss={dismissToast} />
    </div>
  </RequireAuth>
  );
}
