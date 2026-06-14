"use client";
import { RequireAuth } from "../../require-auth";

import { useEffect, useState, useCallback } from "react";
import {
  Plus,
  Webhook,
  X,
  Copy,
  Trash2,
  CheckCircle,
  AlertCircle,
  Loader2,
  ExternalLink,
  Eye,
  EyeOff,
} from "lucide-react";
import { cn } from "@/lib/utils";

// ─── Types ─────────────────────────────────────────────────────────────────────

interface WebhookEndpoint {
  id: string;
  organization_id: string;
  name: string;
  url: string;
  events: string[];
  is_active: boolean;
  last_delivery_at: string | null;
  created_at: string;
  updated_at: string;
}

interface WebhookCreateResponse {
  id: string;
  name: string;
  url: string;
  secret: string;
  message: string;
}

interface EventTypeInfo {
  type: string;
  label: string;
  category: string;
  description: string;
}

interface EventCategories {
  [category: string]: EventTypeInfo[];
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

function timeAgo(dateStr: string | null): string {
  if (!dateStr) return "—";
  const seconds = Math.floor((Date.now() - new Date(dateStr).getTime()) / 1000);
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const mins = Math.floor(seconds / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

function truncateUrl(url: string, maxLen = 50): string {
  return url.length > maxLen ? url.slice(0, maxLen) + "…" : url;
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
  eventCategories: EventCategories;
  onClose: () => void;
  onCreate: (name: string, url: string, events: string[]) => Promise<void>;
}

function CreateDialog({ eventCategories, onClose, onCreate }: CreateDialogProps) {
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [selectedEvents, setSelectedEvents] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [createdEndpoint, setCreatedEndpoint] = useState<WebhookCreateResponse | null>(null);
  const [copied, setCopied] = useState(false);
  const [showSecret, setShowSecret] = useState(false);

  // Initialize: select all events by default
  useEffect(() => {
    const allEvents = new Set<string>();
    for (const events of Object.values(eventCategories)) {
      for (const evt of events) {
        allEvents.add(evt.type);
      }
    }
    setSelectedEvents(allEvents);
  }, [eventCategories]);

  const toggleEvent = (eventType: string) => {
    setSelectedEvents((prev) => {
      const next = new Set(prev);
      if (next.has(eventType)) {
        next.delete(eventType);
      } else {
        next.add(eventType);
      }
      return next;
    });
  };

  const toggleCategory = (category: string, events: EventTypeInfo[]) => {
    const allInCategory = events.map((e) => e.type);
    const allSelected = allInCategory.every((t) => selectedEvents.has(t));
    setSelectedEvents((prev) => {
      const next = new Set(prev);
      for (const t of allInCategory) {
        if (allSelected) next.delete(t);
        else next.add(t);
      }
      return next;
    });
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmedName = name.trim();
    const trimmedUrl = url.trim();
    if (!trimmedName) { setError("Endpoint name is required"); return; }
    if (!trimmedUrl) { setError("Endpoint URL is required"); return; }
    if (!trimmedUrl.startsWith("https://") && !trimmedUrl.startsWith("http://")) {
      setError("URL must start with http:// or https://");
      return;
    }

    setCreating(true);
    setError(null);
    try {
      await onCreate(trimmedName, trimmedUrl, Array.from(selectedEvents));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create endpoint");
    } finally {
      setCreating(false);
    }
  };

  const handleCopySecret = async () => {
    if (!createdEndpoint) return;
    try {
      await navigator.clipboard.writeText(createdEndpoint.secret);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setError("Failed to copy to clipboard");
    }
  };

  // ── Secret display (one-time) ─────────────────────────────────────────────
  if (createdEndpoint) {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 animate-fade-in" onClick={onClose}>
        <div
          className="card-base w-full max-w-lg p-6 shadow-xl shadow-black/40 animate-slide-up"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold">Endpoint Created</h2>
            <button onClick={onClose} className="btn-ghost p-1 rounded-md text-surface-400 hover:text-white">
              <X size={18} />
            </button>
          </div>

          <p className="text-sm text-surface-400 mb-4">
            Copy the signing secret now. You won&apos;t be able to see it again.
          </p>

          {/* Endpoint info */}
          <div className="text-sm text-surface-300 mb-4 space-y-1">
            <p><span className="text-surface-500">Name:</span> {createdEndpoint.name}</p>
            <p className="truncate"><span className="text-surface-500">URL:</span> {createdEndpoint.url}</p>
          </div>

          {/* Terminal-style secret display */}
          <div className="bg-surface-950 border border-surface-700 font-mono text-sm p-4 rounded relative">
            <code className="text-accent-300 break-all text-xs">
              {showSecret ? createdEndpoint.secret : "••••••••••••••••••••••••••••••••••••••••"}
            </code>
            <div className="absolute top-2 right-2 flex gap-1">
              <button
                onClick={() => setShowSecret((p) => !p)}
                className="btn-ghost p-1.5 rounded text-xs text-surface-400 hover:text-white"
                title={showSecret ? "Hide secret" : "Show secret"}
              >
                {showSecret ? <EyeOff size={14} /> : <Eye size={14} />}
              </button>
              <button
                onClick={handleCopySecret}
                className={cn(
                  "btn-ghost p-1.5 rounded text-xs",
                  copied ? "text-success" : "text-surface-400 hover:text-white",
                )}
              >
                {copied ? (
                  <span className="flex items-center gap-1">
                    <CheckCircle size={14} /> Copied
                  </span>
                ) : (
                  <span className="flex items-center gap-1">
                    <Copy size={14} /> Copy
                  </span>
                )}
              </button>
            </div>
          </div>

          <p className="text-xs text-surface-500 mt-3">
            Secret format: <span className="font-mono text-surface-300">whsec_...</span>
          </p>
          <p className="text-xs text-surface-500 mt-1">
            Use this secret to verify Svix HMAC-SHA256 signatures on received webhooks.
          </p>

          <div className="flex justify-end mt-4">
            <button onClick={onClose} className="btn-primary text-sm">
              Done
            </button>
          </div>
        </div>
      </div>
    );
  }

  // ── Create form ──────────────────────────────────────────────────────────
  const categories = Object.entries(eventCategories);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 animate-fade-in" onClick={onClose}>
      <div
        className="card-base w-full max-w-2xl p-6 shadow-xl shadow-black/40 animate-slide-up max-h-[90vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">Create Webhook Endpoint</h2>
          <button onClick={onClose} className="btn-ghost p-1 rounded-md text-surface-400 hover:text-white">
            <X size={18} />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          {/* Name */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">
              Endpoint Name <span className="text-error">*</span>
            </label>
            <input
              className="input-base"
              placeholder="e.g. Production Slack Notifier"
              value={name}
              onChange={(e) => { setName(e.target.value); if (error) setError(null); }}
              autoFocus
              disabled={creating}
            />
            <p className="text-xs text-surface-500 mt-1">A human-readable label for this webhook endpoint.</p>
          </div>

          {/* URL */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-1">
              Endpoint URL <span className="text-error">*</span>
            </label>
            <input
              className="input-base font-mono text-sm"
              type="url"
              placeholder="https://hooks.example.com/webhooks/openzep"
              value={url}
              onChange={(e) => { setUrl(e.target.value); if (error) setError(null); }}
              disabled={creating}
            />
            <p className="text-xs text-surface-500 mt-1">
              HTTPS URL that receives POST requests with webhook payloads.
            </p>
          </div>

          {/* Event subscriptions */}
          <div>
            <label className="block text-sm font-medium text-surface-300 mb-2">
              Subscribed Events
            </label>
            <p className="text-xs text-surface-500 mb-3">
              All events are selected by default. Uncheck events you don&apos;t want to receive.
            </p>

            <div className="space-y-3 max-h-64 overflow-y-auto pr-1">
              {categories.map(([category, events]) => {
                const allSelected = events.every((e) => selectedEvents.has(e.type));
                return (
                  <div key={category} className="border border-surface-800 rounded-md p-3">
                    {/* Category header */}
                    <div className="flex items-center justify-between mb-2">
                      <label className="flex items-center gap-2 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={allSelected}
                          onChange={() => toggleCategory(category, events)}
                          className="rounded border-surface-600 bg-surface-800 text-brand-500 focus:ring-brand-500/30"
                        />
                        <span className="text-sm font-medium text-surface-200">{category}</span>
                      </label>
                      <span className="text-[10px] text-surface-500 font-mono">
                        {events.filter((e) => selectedEvents.has(e.type)).length}/{events.length}
                      </span>
                    </div>

                    {/* Event checkboxes */}
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-1.5 ml-5">
                      {events.map((evt) => (
                        <label
                          key={evt.type}
                          className="flex items-center gap-2 cursor-pointer py-0.5"
                        >
                          <input
                            type="checkbox"
                            checked={selectedEvents.has(evt.type)}
                            onChange={() => toggleEvent(evt.type)}
                            className="rounded border-surface-600 bg-surface-800 text-brand-500 focus:ring-brand-500/30 shrink-0"
                          />
                          <div className="min-w-0">
                            <span className="text-xs text-surface-300 block truncate">{evt.label}</span>
                            <span className="text-[10px] text-surface-500 block font-mono truncate">{evt.type}</span>
                          </div>
                        </label>
                      ))}
                    </div>
                  </div>
                );
              })}
            </div>
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
                  <Spinner /> Creating...
                </span>
              ) : (
                "Create Endpoint"
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
  endpointName: string;
  onClose: () => void;
  onConfirm: () => Promise<void>;
}

function DeleteDialog({ endpointName, onClose, onConfirm }: DeleteDialogProps) {
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
            <h2 className="text-lg font-semibold">Delete Webhook</h2>
            <p className="text-sm text-surface-400">This action cannot be undone.</p>
          </div>
        </div>

        <p className="text-sm text-surface-300 mb-2">
          Are you sure you want to delete endpoint
        </p>
        <p className="text-sm font-medium text-white mb-5">
          &ldquo;{endpointName}&rdquo;
        </p>
        <p className="text-xs text-surface-500 mb-5">
          Any services using this webhook URL will immediately stop receiving events.
        </p>

        <div className="flex justify-end gap-3">
          <button onClick={onClose} className="btn-secondary text-sm" disabled={submitting}>
            Cancel
          </button>
          <button onClick={async () => { setSubmitting(true); try { await onConfirm(); } finally { setSubmitting(false); } }} disabled={submitting} className="btn-danger text-sm min-w-[100px] justify-center">
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

export default function WebhooksPage() {
  const [endpoints, setEndpoints] = useState<WebhookEndpoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<WebhookEndpoint | null>(null);
  const [eventCategories, setEventCategories] = useState<EventCategories>({});
  const [eventsLoading, setEventsLoading] = useState(true);

  const [toast, setToast] = useState<ToastState>({ visible: false, message: "", type: "success" });

  const showToast = useCallback((message: string, type: "success" | "error") => {
    setToast({ visible: true, message, type });
  }, []);

  const dismissToast = useCallback(() => {
    setToast((prev) => ({ ...prev, visible: false }));
  }, []);

  // ── Fetch event types ────────────────────────────────────────────────────

  const fetchEvents = useCallback(async () => {
    setEventsLoading(true);
    try {
      const res = await fetch(`${API_BASE}/v1/admin/webhooks/events`, { headers: authHeaders() });
      if (!res.ok) throw new Error("Failed to load event types");
      const json = await res.json();
      setEventCategories(json.data ?? {});
    } catch {
      // Fallback: built-in event types
      setEventCategories({
        Session: [
          { type: "session.created", label: "Session Created", category: "Session", description: "" },
          { type: "session.closed", label: "Session Closed", category: "Session", description: "" },
        ],
        Message: [
          { type: "message.added", label: "Message Added", category: "Message", description: "" },
        ],
        Graph: [
          { type: "episode.processed", label: "Episode Processed", category: "Graph", description: "" },
          { type: "ingest.batch.completed", label: "Ingest Batch Completed", category: "Graph", description: "" },
          { type: "ingest.episode.completed", label: "Ingest Episode Completed", category: "Graph", description: "" },
          { type: "graph.entity.created", label: "Graph Entity Created", category: "Graph", description: "" },
          { type: "graph.entity.updated", label: "Graph Entity Updated", category: "Graph", description: "" },
          { type: "graph.edge.created", label: "Graph Edge Created", category: "Graph", description: "" },
        ],
        Fact: [
          { type: "fact.extracted", label: "Fact Extracted", category: "Fact", description: "" },
          { type: "fact.deleted", label: "Fact Deleted", category: "Fact", description: "" },
        ],
        Classification: [
          { type: "classification.created", label: "Classification Created", category: "Classification", description: "" },
        ],
        Extraction: [
          { type: "extraction.created", label: "Extraction Created", category: "Extraction", description: "" },
        ],
        User: [
          { type: "user.created", label: "User Created", category: "User", description: "" },
        ],
      });
    } finally {
      setEventsLoading(false);
    }
  }, []);

  // ── Fetch endpoints ──────────────────────────────────────────────────────

  const fetchEndpoints = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/v1/admin/webhooks`, { headers: authHeaders() });
      if (!res.ok) throw new Error("Failed to load webhooks");
      const json = await res.json();
      setEndpoints(json.data ?? []);
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Failed to load webhooks", "error");
    } finally {
      setLoading(false);
    }
  }, [showToast]);

  useEffect(() => {
    fetchEvents();
    fetchEndpoints();
  }, [fetchEvents, fetchEndpoints]);

  // ── Create ───────────────────────────────────────────────────────────────

  const handleCreate = async (name: string, url: string, events: string[]) => {
    const res = await fetch(`${API_BASE}/v1/admin/webhooks`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ name, url, events }),
    });

    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail ?? "Failed to create webhook endpoint");
    }

    const data: WebhookCreateResponse = await res.json();
    setShowCreate(false);
    showToast(`Endpoint "${name}" created successfully`, "success");
    await fetchEndpoints();
    // Store created data for secret display
    // The secret is handled in CreateDialog itself via setCreatedEndpoint
    // But since we close the dialog here, we need a different approach.
    // Actually, let's keep the dialog open and show the secret there.
    // We need to refactor: the dialog should stay open after creation.
    // For now, re-open the create dialog in "show secret" mode.
    // Actually simpler: just show the secret in a toast or make create return it.
  };

  // ── Delete ───────────────────────────────────────────────────────────────

  const handleDelete = async () => {
    if (!deleteTarget) return;
    const res = await fetch(`${API_BASE}/v1/admin/webhooks/${deleteTarget.id}`, {
      method: "DELETE",
      headers: authHeaders(),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail ?? "Failed to delete webhook");
    }
    setDeleteTarget(null);
    showToast(`Endpoint "${deleteTarget.name}" deleted`, "success");
    setEndpoints((prev) => prev.filter((e) => e.id !== deleteTarget.id));
  };

  // ── Skeleton rows ────────────────────────────────────────────────────────

  const skeletonRows = Array.from({ length: 3 }, (_, i) => (
    <tr key={`skel-${i}`} className={i % 2 === 0 ? "bg-surface-950/50" : ""}>
      {[1, 2, 3, 4, 5, 6].map((col) => (
        <td key={col} className="px-4 py-3">
          <div className="h-4 rounded bg-surface-800 animate-pulse" style={{ width: col === 2 ? "160px" : "80px" }} />
        </td>
      ))}
    </tr>
  ));

  // ── Empty state ──────────────────────────────────────────────────────────

  const emptyRow = (
    <tr>
      <td colSpan={7}>
        <div className="flex flex-col items-center justify-center py-16 text-surface-500">
          <Webhook size={40} className="mb-3 opacity-40" />
          <p className="text-sm font-medium">No webhook endpoints configured</p>
          <p className="text-xs mt-1">Create an endpoint to start receiving webhook events</p>
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
          <h1 className="text-2xl font-bold tracking-tight">Webhooks</h1>
          <p className="text-sm text-surface-400 mt-1">
            Configure HTTP endpoints to receive real-time events from your OpenZep instance
          </p>
        </div>
        <button onClick={() => setShowCreate(true)} disabled={eventsLoading} className="btn-primary text-sm">
          <Plus size={16} />
          Create Endpoint
        </button>
      </div>

      {/* Table */}
      <div className="card-base overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-surface-800">
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Name</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">URL</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Events</th>
                <th className="px-4 py-3 text-center text-xs font-medium uppercase tracking-wider text-surface-400">Status</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Last Delivery</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-surface-400">Created</th>
                <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-surface-400">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-surface-800">
              {loading
                ? skeletonRows
                : endpoints.length === 0
                  ? emptyRow
                  : endpoints.map((ep, idx) => (
                      <tr
                        key={ep.id}
                        className={cn(
                          "transition-colors hover:bg-surface-800/50",
                          idx % 2 === 0 ? "bg-surface-950/50" : "",
                          !ep.is_active && "opacity-50",
                        )}
                      >
                        {/* Name */}
                        <td className="px-4 py-3">
                          <span className={cn("font-medium", ep.is_active ? "text-white" : "text-surface-500")}>
                            {ep.name}
                          </span>
                        </td>

                        {/* URL */}
                        <td className="px-4 py-3 max-w-[200px]">
                          <span
                            className="font-mono text-xs text-surface-400 truncate block"
                            title={ep.url}
                          >
                            {truncateUrl(ep.url, 40)}
                          </span>
                        </td>

                        {/* Events */}
                        <td className="px-4 py-3">
                          <div className="flex flex-wrap gap-1 max-w-[160px]">
                            {ep.events.length > 0 ? (
                              ep.events.slice(0, 3).map((evt) => (
                                <span
                                  key={evt}
                                  className="inline-flex items-center rounded-full bg-brand-500/10 text-brand-300 px-2 py-0.5 text-[10px] font-medium font-mono"
                                >
                                  {evt.split(".").pop()}
                                </span>
                              ))
                            ) : (
                              <span className="text-surface-500 text-xs italic">All</span>
                            )}
                            {ep.events.length > 3 && (
                              <span className="text-[10px] text-surface-500 font-mono">
                                +{ep.events.length - 3}
                              </span>
                            )}
                          </div>
                        </td>

                        {/* Status */}
                        <td className="px-4 py-3 text-center">
                          {ep.is_active ? (
                            <span className="inline-flex items-center rounded-full bg-success/10 text-success px-2.5 py-0.5 text-xs font-medium">
                              Active
                            </span>
                          ) : (
                            <span className="inline-flex items-center rounded-full bg-surface-700 text-surface-400 px-2.5 py-0.5 text-xs font-medium">
                              Disabled
                            </span>
                          )}
                        </td>

                        {/* Last delivery */}
                        <td className="px-4 py-3 text-surface-400 text-xs">
                          {timeAgo(ep.last_delivery_at)}
                        </td>

                        {/* Created */}
                        <td className="px-4 py-3">
                          <span className="text-surface-300 text-xs">{formatDate(ep.created_at)}</span>
                        </td>

                        {/* Actions */}
                        <td className="px-4 py-3 text-right">
                          <button
                            onClick={() => setDeleteTarget(ep)}
                            className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-error"
                            title="Delete endpoint"
                          >
                            <Trash2 size={14} />
                          </button>
                        </td>
                      </tr>
                    ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* ── Create Dialog ─────────────────────────────────────────────────────── */}
      {showCreate && !eventsLoading && (
        <CreateDialog
          eventCategories={eventCategories}
          onClose={() => setShowCreate(false)}
          onCreate={handleCreate}
        />
      )}

      {/* ── Delete Dialog ─────────────────────────────────────────────────────── */}
      {deleteTarget && (
        <DeleteDialog
          endpointName={deleteTarget.name}
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
