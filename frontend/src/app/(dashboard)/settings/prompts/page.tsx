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
import {
  get,
  post,
  put,
  del as apiDel,
  getAccessToken,
  API_BASE,
  ApiError,
} from "@/lib/api-client";
import { timeAgo, formatDate } from "@/lib/utils";
import { toast } from "sonner";
import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/empty-state";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Spinner } from "@/components/ui/spinner";
import { TableSkeleton } from "@/components/shared/skeleton";

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

// ─── Helpers ───────────────────────────────────────────────────────────────────

function templateDisplayName(name: string): string {
  return name
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

const KNOWN_TYPES = [
  { value: "fact_extraction", label: "Fact Extraction" },
  { value: "entity_extraction", label: "Entity Extraction" },
  { value: "classification", label: "Classification" },
  { value: "structured_extraction", label: "Structured Extraction" },
  { value: "user_summary", label: "User Summary" },
];

// ─── Auth fetch helper for dialogs (kept as-is to avoid breaking nested logic) ─

function authHeaders(): Record<string, string> {
  const token = getAccessToken();
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  return headers;
}

// ═══════════════════════════════════════════════════════════════════════════════
// Child dialogs — each has unique business logic, kept mostly as-is
// ═══════════════════════════════════════════════════════════════════════════════

// ─── Edit Dialog ───────────────────────────────────────────────────────────────

function EditDialog({
  template,
  onClose,
  onSaved,
  onReset,
  onShowHistory,
}: {
  template: PromptTemplateDetail;
  onClose: () => void;
  onSaved: () => Promise<void>;
  onReset: () => Promise<void>;
  onShowHistory: () => void;
}) {
  const [templateText, setTemplateText] = useState(template.template_text);
  const [description, setDescription] = useState(template.description ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showResetConfirm, setShowResetConfirm] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [settingDefault, setSettingDefault] = useState(false);

  useEffect(() => {
    setTemplateText(template.template_text);
    setDescription(template.description ?? "");
    setError(null);
    setShowResetConfirm(false);
  }, [template]);

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!templateText.trim()) { setError("Template text cannot be empty"); return; }
    setSaving(true); setError(null);
    try {
      const res = await fetch(`${API_BASE}/admin/org/prompts/${template.name}`, {
        method: "PUT",
        headers: authHeaders(),
        body: JSON.stringify({ template_text: templateText, description: description.trim() || null }),
      });
      if (!res.ok) { const b = await res.json().catch(() => ({})); throw new Error(b.detail ?? "Failed to save template"); }
      toast.success(`Template "${templateDisplayName(template.name)}" saved as new version`);
      await onSaved();
    } catch (err) { setError(err instanceof Error ? err.message : "Failed to save template"); }
    finally { setSaving(false); }
  };

  const handleReset = async () => {
    setResetting(true);
    try {
      const res = await fetch(`${API_BASE}/admin/org/prompts/${template.name}`, { method: "DELETE", headers: authHeaders() });
      if (!res.ok) { const b = await res.json().catch(() => ({})); throw new Error(b.detail ?? "Failed to reset template"); }
      setShowResetConfirm(false);
      toast.success(`Template "${templateDisplayName(template.name)}" reset to default`);
      await onReset();
    } catch (err) { setError(err instanceof Error ? err.message : "Failed to reset template"); }
    finally { setResetting(false); }
  };

  const handleSetDefault = async () => {
    if (!template.type || template.is_default_for_type) return;
    setSettingDefault(true); setError(null);
    try {
      const res = await fetch(`${API_BASE}/admin/org/prompts/${template.name}/set-default`, { method: "POST", headers: authHeaders() });
      if (!res.ok) { const b = await res.json().catch(() => ({})); throw new Error(b.detail ?? "Failed to set as default"); }
      toast.success(`"${templateDisplayName(template.name)}" is now the default for its type`);
      await onSaved();
    } catch (err) { setError(err instanceof Error ? err.message : "Failed to set as default"); }
    finally { setSettingDefault(false); }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 animate-fade-in" onClick={onClose}>
      <div className="card-base w-full max-w-3xl p-6 shadow-xl shadow-black/40 animate-slide-up max-h-[90vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-md bg-brand-500/10 shrink-0"><FileText size={16} className="text-brand-300" /></div>
            <div><h2 className="text-lg font-semibold">{templateDisplayName(template.name)}</h2><p className="text-xs text-surface-400">Version {template.version}</p></div>
          </div>
          <button onClick={onClose} className="btn-ghost p-1 rounded-md text-surface-400 hover:text-white"><X size={18} /></button>
        </div>
        <div className="flex items-start gap-2 rounded-lg border border-amber-500/20 bg-amber-500/5 px-3 py-2 mb-4">
          <AlertCircle size={13} className="text-amber-400 mt-0.5 shrink-0" />
          <p className="text-xs text-amber-200/80 leading-relaxed">Custom prompts override the system default. Incorrect Jinja2 syntax may cause extraction failures.</p>
        </div>
        <form onSubmit={handleSave} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Description <span className="text-surface-500">(optional)</span></label>
            <input className="input-base" placeholder="Describe what this template does" value={description} onChange={(e) => { setDescription(e.target.value); if (error) setError(null); }} disabled={saving} />
          </div>
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Template <span className="text-error">*</span></label>
            <textarea
              className={cn("w-full rounded-lg border border-surface-700 bg-surface-950 p-4 text-sm font-mono leading-relaxed text-surface-100 placeholder-surface-500 outline-none resize-y min-h-[300px] transition-all duration-150 focus:border-accent-300 focus:shadow-[0_0_0_2px_rgba(143,175,217,0.2)]")}
              placeholder="{% raw %}{{ Enter your Jinja2 template here }}{% endraw %}"
              value={templateText} onChange={(e) => { setTemplateText(e.target.value); if (error) setError(null); }} disabled={saving} spellCheck={false}
            />
            <p className="text-xs text-surface-500 mt-1">Jinja2 template syntax. Use {"{{ variables }}"} and {"{% tags %}"} for dynamic content.</p>
          </div>
          {error && (<div className="rounded-md bg-error/10 border border-error/30 px-3 py-2 text-sm text-error flex items-center gap-2"><AlertCircle size={14} />{error}</div>)}
          <div className="flex items-center justify-between pt-2 border-t border-surface-800">
            <div className="flex items-center gap-2">
              <button type="button" onClick={onShowHistory} className="btn-ghost text-sm"><History size={14} /> Version History</button>
              {template.type && !template.is_default_for_type && (
                <button type="button" onClick={handleSetDefault} disabled={settingDefault} className="btn-ghost text-xs text-amber-400 hover:text-amber-300">
                  {settingDefault ? <Spinner className="text-amber-400" /> : <><Star size={12} /> Set as Default</>}
                </button>
              )}
            </div>
            <div className="flex items-center gap-3">
              {template.is_customised && (
                <>
                  {showResetConfirm ? (
                    <div className="flex items-center gap-2">
                      <span className="text-xs text-surface-400">Reset to default?</span>
                      <button type="button" onClick={handleReset} disabled={resetting} className="btn-danger text-xs min-w-[80px] justify-center">
                        {resetting ? <span className="flex items-center gap-1"><Spinner /> Resetting...</span> : <span className="flex items-center gap-1"><Trash2 size={12} /> Confirm</span>}
                      </button>
                      <button type="button" onClick={() => setShowResetConfirm(false)} className="btn-ghost text-xs" disabled={resetting}>Cancel</button>
                    </div>
                  ) : (
                    <button type="button" onClick={() => setShowResetConfirm(true)} className="btn-ghost text-xs text-surface-400 hover:text-error"><RotateCcw size={12} /> Reset to Default</button>
                  )}
                </>
              )}
              <button type="button" onClick={onClose} className="btn-secondary text-sm" disabled={saving}>Cancel</button>
              <button type="submit" disabled={saving || (!templateText.trim() && !error)} className="btn-primary text-sm min-w-[120px] justify-center">
                {saving ? <span className="flex items-center gap-2"><Spinner /> Saving...</span> : <span className="flex items-center gap-2"><Save size={14} /> Save as New Version</span>}
              </button>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}

// ─── Version History Dialog ────────────────────────────────────────────────────

function VersionHistoryDialog({
  templateName,
  onClose,
  onRollback,
}: {
  templateName: string;
  onClose: () => void;
  onRollback: (version: number) => Promise<void>;
}) {
  const [versionsData, setVersionsData] = useState<PromptTemplateVersions | null>(null);
  const [loading, setLoading] = useState(true);
  const [selectedVersion, setSelectedVersion] = useState<PromptTemplateDetail | null>(null);
  const [rollingBack, setRollingBack] = useState(false);
  const [promoting, setPromoting] = useState(false);
  const [promoteConfirm, setPromoteConfirm] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fetchVersions = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const res = await fetch(`${API_BASE}/admin/org/prompts/${templateName}/versions`, { headers: authHeaders() });
      if (!res.ok) throw new Error("Failed to load version history");
      const data: PromptTemplateVersions = await res.json();
      setVersionsData(data);
      const active = data.versions.find((v) => v.is_active);
      if (active) setSelectedVersion(active);
    } catch (err) { setError(err instanceof Error ? err.message : "Failed to load version history"); }
    finally { setLoading(false); }
  }, [templateName]);

  useEffect(() => { fetchVersions(); }, [fetchVersions]);

  const handleRollback = async (version: number) => {
    setRollingBack(true);
    try {
      const res = await fetch(`${API_BASE}/admin/org/prompts/${templateName}/rollback/${version}`, { method: "POST", headers: authHeaders() });
      if (!res.ok) { const b = await res.json().catch(() => ({})); throw new Error(b.detail ?? "Failed to rollback template"); }
      toast.success(`Template "${templateDisplayName(templateName)}" rolled back to version ${version}`);
      await fetchVersions();
      onRollback(version);
    } catch (err) { setError(err instanceof Error ? err.message : "Failed to rollback template"); }
    finally { setRollingBack(false); }
  };

  const handlePromote = async (version: number) => {
    setPromoting(true); setPromoteConfirm(null);
    try {
      const res = await fetch(`${API_BASE}/admin/org/prompts/${templateName}/promote/${version}`, { method: "POST", headers: authHeaders() });
      if (!res.ok) { const b = await res.json().catch(() => ({})); throw new Error(b.detail ?? "Failed to promote template"); }
      toast.success(`Version ${version} of "${templateDisplayName(templateName)}" is now the system default`);
      await fetchVersions();
    } catch (err) { setError(err instanceof Error ? err.message : "Failed to promote template"); }
    finally { setPromoting(false); }
  };

  const isActiveVersion = (version: number) => versionsData?.current_version === version;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 animate-fade-in" onClick={onClose}>
      <div className="card-base w-full max-w-4xl p-6 shadow-xl shadow-black/40 animate-slide-up max-h-[90vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4 shrink-0">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-md bg-surface-800 shrink-0"><History size={16} className="text-surface-300" /></div>
            <div><h2 className="text-lg font-semibold">Version History</h2><p className="text-xs text-surface-400">{templateDisplayName(templateName)}</p></div>
          </div>
          <button onClick={onClose} className="btn-ghost p-1 rounded-md text-surface-400 hover:text-white"><X size={18} /></button>
        </div>
        {error && (<div className="rounded-md bg-error/10 border border-error/30 px-3 py-2 text-sm text-error flex items-center gap-2 mb-4 shrink-0"><AlertCircle size={14} />{error}</div>)}
        <div className="flex gap-4 flex-1 min-h-0">
          <div className="w-56 shrink-0 overflow-y-auto border border-surface-800 rounded-md">
            {loading ? (
              <div className="flex items-center justify-center py-12"><Spinner className="text-surface-400" /></div>
            ) : !versionsData || versionsData.versions.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-12 text-surface-500"><History size={24} className="mb-2 opacity-40" /><p className="text-xs">No versions found</p></div>
            ) : (
              <div className="divide-y divide-surface-800">
                {versionsData.versions.map((v) => {
                  const isActive = isActiveVersion(v.version);
                  return (
                    <button key={v.version} onClick={() => setSelectedVersion(v)} className={cn("w-full text-left px-3 py-2.5 transition-colors hover:bg-surface-800/50", selectedVersion?.version === v.version && "bg-surface-800")}>
                      <div className="flex items-center justify-between">
                        <span className={cn("text-sm font-medium", isActive ? "text-success" : "text-surface-300")}>v{v.version}</span>
                        {v.is_system_default && <span className="text-[10px] font-medium text-amber-400">Default</span>}
                        {isActive && !v.is_system_default && <span className="text-[10px] font-medium text-success">Active</span>}
                      </div>
                      <p className="text-[11px] text-surface-500 mt-0.5 truncate">{v.description || "No description"}</p>
                    </button>
                  );
                })}
              </div>
            )}
          </div>
          <div className="flex-1 flex flex-col min-w-0">
            {selectedVersion ? (
              <>
                <div className="flex items-center justify-between mb-2 shrink-0">
                  <div className="flex items-center gap-2">
                    <Eye size={14} className="text-surface-400" />
                    <span className="text-sm font-medium text-surface-200">Version {selectedVersion.version}</span>
                    {isActiveVersion(selectedVersion.version) && <Badge variant="success" size="sm">Currently Active</Badge>}
                  </div>
                  <div className="flex items-center gap-2">
                    {!isActiveVersion(selectedVersion.version) && (
                      <button onClick={() => handleRollback(selectedVersion.version)} disabled={rollingBack} className="btn-primary text-xs min-w-[100px] justify-center">
                        {rollingBack ? <span className="flex items-center gap-1"><Spinner /> Rolling back...</span> : <span className="flex items-center gap-1"><RotateCcw size={12} /> Rollback to v{selectedVersion.version}</span>}
                      </button>
                    )}
                    {!selectedVersion.is_system_default ? (
                      promoteConfirm === selectedVersion.version ? (
                        <div className="flex items-center gap-2">
                          <span className="text-xs text-amber-400">Set as global default?</span>
                          <button onClick={() => handlePromote(selectedVersion.version)} disabled={promoting} className="btn-primary text-xs bg-amber-500/20 border-amber-500/30 hover:bg-amber-500/30 min-w-[80px] justify-center">
                            {promoting ? <span className="flex items-center gap-1"><Spinner /> Promoting...</span> : <span className="flex items-center gap-1"><Star size={12} /> Confirm</span>}
                          </button>
                          <button onClick={() => setPromoteConfirm(null)} className="btn-ghost text-xs text-surface-400" disabled={promoting}>Cancel</button>
                        </div>
                      ) : (
                        <button onClick={() => setPromoteConfirm(selectedVersion.version)} className="btn-ghost text-xs text-amber-400 hover:text-amber-300"><Star size={12} /> Set as System Default</button>
                      )
                    ) : (
                      <span className="inline-flex items-center gap-1 text-xs text-amber-400"><Star size={12} /> System Default</span>
                    )}
                  </div>
                </div>
                <div className="relative flex-1">
                  <pre className="absolute inset-0 overflow-auto rounded-lg border border-surface-700 bg-surface-950 p-4 text-xs font-mono leading-relaxed text-surface-100 whitespace-pre-wrap break-all">{selectedVersion.template_text}</pre>
                </div>
              </>
            ) : (
              <div className="flex flex-col items-center justify-center h-full text-surface-500"><Eye size={32} className="mb-2 opacity-40" /><p className="text-sm">Select a version to preview</p></div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── Browse System Prompts Dialog ──────────────────────────────────────────────

interface SystemTemplateEntry {
  name: string; version: number; type: string | null; is_active: boolean;
  is_default_for_type: boolean; is_system_default: boolean; description: string | null;
}
interface SystemPromptGroup { type: string; templates: SystemTemplateEntry[]; imported: string[]; }

function BrowserDialog({ onClose, onImported }: { onClose: () => void; onImported: () => void }) {
  const [groups, setGroups] = useState<SystemPromptGroup[]>([]);
  const [loading, setLoading] = useState(true);
  const [importing, setImporting] = useState<string | null>(null);

  const fetchSystemPrompts = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/admin/org/prompts/system`, { headers: authHeaders() });
      if (!res.ok) throw new Error("Failed to load system prompts");
      const data: { groups: SystemPromptGroup[] } = await res.json();
      setGroups(data.groups ?? []);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to load system prompts");
      onClose();
    } finally { setLoading(false); }
  }, [onClose]);

  useEffect(() => { fetchSystemPrompts(); }, [fetchSystemPrompts]);

  const handleImport = async (templateName: string) => {
    setImporting(templateName);
    try {
      const res = await fetch(`${API_BASE}/admin/org/prompts/import`, { method: "POST", headers: authHeaders(), body: JSON.stringify({ template_name: templateName }) });
      if (!res.ok) { const b = await res.json().catch(() => ({})); throw new Error(b.detail ?? "Failed to import template"); }
      toast.success(`"${templateDisplayName(templateName)}" imported successfully`);
      await fetchSystemPrompts();
      onImported();
    } catch (err) { toast.error(err instanceof Error ? err.message : "Failed to import template"); }
    finally { setImporting(null); }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 animate-fade-in" onClick={onClose}>
      <div className="card-base w-full max-w-2xl p-6 shadow-xl shadow-black/40 animate-slide-up max-h-[85vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4 shrink-0">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-md bg-surface-800 shrink-0"><Download size={16} className="text-surface-300" /></div>
            <div><h2 className="text-lg font-semibold">Browse System Prompts</h2><p className="text-xs text-surface-400">Import system-default prompt templates into your organisation</p></div>
          </div>
          <button onClick={onClose} className="btn-ghost p-1 rounded-md text-surface-400 hover:text-white"><X size={18} /></button>
        </div>
        <div className="flex-1 overflow-y-auto space-y-4 min-h-0">
          {loading ? (
            <div className="flex items-center justify-center py-12"><Spinner className="text-surface-400" /></div>
          ) : groups.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 text-surface-500"><Download size={32} className="mb-2 opacity-40" /><p className="text-sm">No system prompts available</p></div>
          ) : (
            groups.map((group) => (
              <div key={group.type} className="border border-surface-800 rounded-md overflow-hidden">
                <div className="bg-surface-800/50 px-3 py-2 text-sm font-medium text-surface-200">{templateDisplayName(group.type)}</div>
                <div className="divide-y divide-surface-800">
                  {group.templates.map((t) => {
                    const isImported = group.imported.includes(t.name);
                    return (
                      <div key={t.name} className="flex items-center justify-between px-3 py-2.5 hover:bg-surface-800/30">
                        <div className="flex items-center gap-2">
                          <span className="text-sm font-mono text-surface-200">{t.name}</span>
                          <span className="text-[10px] text-surface-500">v{t.version}</span>
                          {t.is_system_default && <Badge variant="warning" size="sm"><Star size={10} /> Active Default</Badge>}
                          {!t.is_active && !t.is_system_default && <span className="text-[10px] text-surface-500">inactive</span>}
                        </div>
                        {isImported ? (
                          <span className="inline-flex items-center gap-1 text-xs text-success"><CheckCircle size={12} /> Imported</span>
                        ) : (
                          <button onClick={() => handleImport(t.name)} disabled={importing === t.name} className="btn-primary text-xs min-w-[80px] justify-center">
                            {importing === t.name ? <span className="flex items-center gap-1"><Spinner /> Importing...</span> : "Import"}
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

// ─── Create Dialog ─────────────────────────────────────────────────────────────

function CreateDialog({ onClose, onCreate }: { onClose: () => void; onCreate: () => Promise<void> }) {
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
    if (!type) { setError("Template type is required"); return; }
    if (!trimmedText) { setError("Template text is required"); return; }
    setCreating(true); setError(null);
    try {
      const res = await fetch(`${API_BASE}/admin/org/prompts/${encodeURIComponent(trimmedName)}`, {
        method: "PUT", headers: authHeaders(),
        body: JSON.stringify({ template_text: trimmedText, description: description.trim() || null, type }),
      });
      if (!res.ok) { const b = await res.json().catch(() => ({})); throw new Error(b.detail ?? "Failed to create template"); }
      toast.success(`Template "${templateDisplayName(trimmedName)}" created`);
      await onCreate();
    } catch (err) { setError(err instanceof Error ? err.message : "Failed to create template"); }
    finally { setCreating(false); }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 animate-fade-in" onClick={onClose}>
      <div className="card-base w-full max-w-3xl p-6 shadow-xl shadow-black/40 animate-slide-up max-h-[90vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-md bg-brand-500/10 shrink-0"><FileText size={16} className="text-brand-300" /></div>
            <div><h2 className="text-lg font-semibold">New Prompt Template</h2><p className="text-xs text-surface-400">Create a custom prompt template for your organisation</p></div>
          </div>
          <button onClick={onClose} className="btn-ghost p-1 rounded-md text-surface-400 hover:text-white"><X size={18} /></button>
        </div>
        <div className="flex items-start gap-2 rounded-lg border border-amber-500/20 bg-amber-500/5 px-3 py-2 mb-4">
          <AlertCircle size={13} className="text-amber-400 mt-0.5 shrink-0" />
          <p className="text-xs text-amber-200/80 leading-relaxed">Incorrect Jinja2 syntax may cause extraction failures. Use the existing templates as reference.</p>
        </div>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Template Name <span className="text-error">*</span></label>
            <input className="input-base" placeholder="e.g. my_custom_ner_v1" value={name} onChange={(e) => { setName(e.target.value); if (error) setError(null); }} autoFocus disabled={creating} />
            <p className="text-xs text-surface-500 mt-1">Unique identifier used as the template key in the system.</p>
          </div>
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Type <span className="text-error">*</span></label>
            <select className="input-base" value={type} onChange={(e) => { setType(e.target.value); if (error) setError(null); }} disabled={creating}>
              <option value="">Select a type…</option>
              {KNOWN_TYPES.map((t) => (<option key={t.value} value={t.value}>{t.label}</option>))}
            </select>
          </div>
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Description <span className="text-surface-500">(optional)</span></label>
            <input className="input-base" placeholder="Describe what this template does" value={description} onChange={(e) => { setDescription(e.target.value); if (error) setError(null); }} disabled={creating} />
          </div>
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">Template <span className="text-error">*</span></label>
            <textarea className="w-full rounded-lg border border-surface-700 bg-surface-950 p-4 text-sm font-mono leading-relaxed text-surface-100 placeholder-surface-500 outline-none resize-y min-h-[300px] transition-all duration-150 focus:border-accent-300 focus:shadow-[0_0_0_2px_rgba(143,175,217,0.2)]" placeholder="{% raw %}{{ Enter your Jinja2 template here }}{% endraw %}" value={templateText} onChange={(e) => { setTemplateText(e.target.value); if (error) setError(null); }} disabled={creating} spellCheck={false} />
          </div>
          {error && (<div className="rounded-md bg-error/10 border border-error/30 px-3 py-2 text-sm text-error flex items-center gap-2"><AlertCircle size={14} />{error}</div>)}
          <div className="flex items-center justify-end gap-3 pt-2 border-t border-surface-800">
            <button type="button" onClick={onClose} className="btn-secondary text-sm" disabled={creating}>Cancel</button>
            <button type="submit" disabled={creating || !name.trim() || !type || !templateText.trim()} className="btn-primary text-sm min-w-[140px] justify-center">
              {creating ? <span className="flex items-center gap-2"><Spinner /> Creating...</span> : <span className="flex items-center gap-2"><Plus size={14} /> Create Template</span>}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ─── Delete Dialog ─────────────────────────────────────────────────────────────

function DeleteDialog({ templateName, templateDisplay, onClose, onConfirm }: {
  templateName: string; templateDisplay: string; onClose: () => void; onConfirm: () => Promise<void>;
}) {
  const [submitting, setSubmitting] = useState(false);
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 animate-fade-in" onClick={onClose}>
      <div className="card-base w-full max-w-sm p-6 shadow-xl shadow-black/40 animate-slide-up" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-3 mb-4">
          <div className="flex h-10 w-10 items-center justify-center rounded-full bg-error/10 shrink-0"><Trash2 size={18} className="text-error" /></div>
          <div><h2 className="text-lg font-semibold">Delete Template</h2><p className="text-sm text-surface-400">This action cannot be undone.</p></div>
        </div>
        <p className="text-sm text-surface-300 mb-2">Are you sure you want to delete</p>
        <p className="text-sm font-medium text-white mb-5">&ldquo;{templateDisplay}&rdquo;</p>
        <p className="text-xs text-surface-500 mb-5">All custom versions will be removed. The organisation will fall back to the system default.</p>
        <div className="flex justify-end gap-3">
          <button onClick={onClose} className="btn-secondary text-sm" disabled={submitting}>Cancel</button>
          <button onClick={async () => { setSubmitting(true); try { await onConfirm(); } finally { setSubmitting(false); } }} disabled={submitting} className="btn-danger text-sm min-w-[100px] justify-center">
            {submitting ? <span className="flex items-center gap-2"><Spinner /> Deleting...</span> : <span className="flex items-center gap-2"><Trash2 size={14} /> Delete</span>}
          </button>
        </div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// Main Page
// ═══════════════════════════════════════════════════════════════════════════════

export default function PromptsPage() {
  const [templates, setTemplates] = useState<PromptTemplateSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [editTarget, setEditTarget] = useState<PromptTemplateDetail | null>(null);
  const [historyTarget, setHistoryTarget] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [showBrowser, setShowBrowser] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [settingDefault, setSettingDefault] = useState<string | null>(null);

  // ── Fetch templates ──────────────────────────────────────────────────────

  const fetchTemplates = useCallback(async () => {
    setLoading(true);
    try {
      const data = await get<{ data: PromptTemplateSummary[] }>("/admin/org/prompts");
      setTemplates(data.data ?? []);
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Failed to load prompt templates");
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { fetchTemplates(); }, [fetchTemplates]);

  // ── Fetch single template for editing ────────────────────────────────────

  const openEditor = useCallback(async (name: string, isCustomised = false) => {
    try {
      const data = await get<PromptTemplateDetail>(`/admin/org/prompts/${name}`);
      setEditTarget({ ...data, is_customised: isCustomised });
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Failed to load template");
    }
  }, []);

  const handleSaved = useCallback(async () => {
    await fetchTemplates();
    if (editTarget) await openEditor(editTarget.name, editTarget.is_customised);
  }, [fetchTemplates, editTarget, openEditor]);

  const handleReset = useCallback(async () => {
    setEditTarget(null);
    await fetchTemplates();
  }, [fetchTemplates]);

  const handleRollback = useCallback(async (_version: number) => {
    setHistoryTarget(null);
    await fetchTemplates();
  }, [fetchTemplates]);

  const handleSetDefault = useCallback(async (name: string) => {
    setSettingDefault(name);
    try {
      await post(`/admin/org/prompts/${name}/set-default`);
      toast.success(`"${templateDisplayName(name)}" is now the default for its type`);
      await fetchTemplates();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Failed to set as default");
    } finally { setSettingDefault(null); }
  }, [fetchTemplates]);

  const handleDelete = useCallback(async () => {
    if (!deleteTarget) return;
    try {
      await apiDel(`/admin/org/prompts/${deleteTarget}`);
      toast.success(`"${templateDisplayName(deleteTarget)}" reverted to system default`);
      setDeleteTarget(null);
      await fetchTemplates();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Failed to delete template");
      setDeleteTarget(null);
    }
  }, [deleteTarget, fetchTemplates]);

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
    <RequireAuth>
    <div className="space-y-6">
      <PageHeader
        title="Prompt Templates"
        description="Manage Jinja2 prompt templates for extraction workers. Organised by type with version history and rollback support."
        actions={
          <div className="flex items-center gap-3">
            <Button variant="primary" size="sm" icon={<Plus size={14} />} onClick={() => setShowCreate(true)}>New Template</Button>
            <Button variant="secondary" size="sm" icon={<Download size={14} />} onClick={() => setShowBrowser(true)}>Browse System Prompts</Button>
          </div>
        }
      />

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
                <TableSkeleton rows={4} cols={5} colWidths={["w-36", "w-12", "w-24", "w-20", "w-32"]} />
              ) : templates.length === 0 ? (
                <tr>
                  <td colSpan={5}>
                    <EmptyState icon={FileText} title="No prompt templates available" description="Prompt templates are loaded from the server" />
                  </td>
                </tr>
              ) : (
                (() => {
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
                      rows.push(<tr key={`group-${currentType ?? "untagged"}`}><td colSpan={5} className="px-4 py-1.5 bg-surface-800/70"><span className="text-xs font-medium text-surface-300">{currentType ? templateDisplayName(currentType) : "Other"}</span></td></tr>);
                    }
                    rows.push(
                      <tr key={tmpl.name} className="transition-colors hover:bg-surface-800/50">
                        <td className="px-4 py-3">
                          <div>
                            <span className="font-medium text-white">{templateDisplayName(tmpl.name)}</span>
                            {tmpl.description && <p className="text-[11px] text-surface-500 mt-0.5 truncate max-w-[200px]">{tmpl.description}</p>}
                          </div>
                        </td>
                        <td className="px-4 py-3"><span className="font-mono text-xs text-surface-300">v{tmpl.version}</span></td>
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-2">
                            {tmpl.type ? <Badge variant="brand" size="sm">{templateDisplayName(tmpl.type)}</Badge> : <span className="text-[11px] text-surface-500">—</span>}
                            {tmpl.is_default_for_type && <Badge variant="warning" size="sm"><Star size={10} /> Default</Badge>}
                          </div>
                        </td>
                        <td className="px-4 py-3">
                          <span className="text-surface-400 text-xs" title={formatDate(tmpl.updated_at)}>{timeAgo(tmpl.updated_at)}</span>
                        </td>
                        <td className="px-4 py-3 text-right">
                          <div className="flex items-center justify-end gap-1">
                            {tmpl.type && !tmpl.is_default_for_type && (
                              <button onClick={() => handleSetDefault(tmpl.name)} disabled={settingDefault === tmpl.name}
                                className="btn-ghost p-1.5 rounded-md text-amber-400 hover:text-amber-300" title="Set as default for this type">
                                {settingDefault === tmpl.name ? <Spinner className="text-amber-400" /> : <Star size={14} />}
                              </button>
                            )}
                            <button onClick={() => openEditor(tmpl.name, tmpl.is_customised)} className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-brand-300" title="Edit template"><Edit3 size={14} /></button>
                            <button onClick={() => setHistoryTarget(tmpl.name)} className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-surface-200" title="View version history"><History size={14} /></button>
                            {tmpl.is_customised && !tmpl.is_default_for_type && (
                              <button onClick={() => setDeleteTarget(tmpl.name)} className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-error" title="Delete template"><Trash2 size={14} /></button>
                            )}
                          </div>
                        </td>
                      </tr>
                    );
                  });
                  return rows;
                })()
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Dialogs */}
      {editTarget && <EditDialog template={editTarget} onClose={() => setEditTarget(null)} onSaved={handleSaved} onReset={handleReset} onShowHistory={() => { const n = editTarget.name; setEditTarget(null); setTimeout(() => setHistoryTarget(n), 100); }} />}
      {historyTarget && <VersionHistoryDialog templateName={historyTarget} onClose={() => setHistoryTarget(null)} onRollback={handleRollback} />}
      {showCreate && <CreateDialog onClose={() => setShowCreate(false)} onCreate={() => { setShowCreate(false); return fetchTemplates(); }} />}
      {deleteTarget && <DeleteDialog templateName={deleteTarget} templateDisplay={templateDisplayName(deleteTarget)} onClose={() => setDeleteTarget(null)} onConfirm={handleDelete} />}
      {showBrowser && <BrowserDialog onClose={() => setShowBrowser(false)} onImported={fetchTemplates} />}
    </div>
  </RequireAuth>
  );
}
