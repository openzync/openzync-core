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
  Eye,
  EyeOff,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { toast } from "sonner";
import { get, post, del, ApiError } from "@/lib/api-client";
import { timeAgo, formatDate } from "@/lib/utils";
import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/empty-state";
import { ErrorState } from "@/components/shared/error-state";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { TableSkeleton } from "@/components/shared/skeleton";

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

// ─── Fallback events ───────────────────────────────────────────────────────────

const FALLBACK_EVENTS: EventCategories = {
  Session: [
    { type: "session.created", label: "Session Created", category: "Session", description: "" },
    { type: "session.closed", label: "Session Closed", category: "Session", description: "" },
  ],
  Message: [{ type: "message.added", label: "Message Added", category: "Message", description: "" }],
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
  Classification: [{ type: "classification.created", label: "Classification Created", category: "Classification", description: "" }],
  Extraction: [{ type: "extraction.created", label: "Extraction Created", category: "Extraction", description: "" }],
  User: [{ type: "user.created", label: "User Created", category: "User", description: "" }],
};

// ─── Helpers ───────────────────────────────────────────────────────────────────

function truncateUrl(url: string, maxLen = 40): string {
  return url.length > maxLen ? url.slice(0, maxLen) + "…" : url;
}

// ─── Create Dialog ─────────────────────────────────────────────────────────────

function CreateDialog({
  eventCategories,
  onClose,
  onCreate,
}: {
  eventCategories: EventCategories;
  onClose: () => void;
  onCreate: (name: string, url: string, events: string[]) => Promise<WebhookCreateResponse>;
}) {
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [selectedEvents, setSelectedEvents] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [createdEndpoint, setCreatedEndpoint] = useState<WebhookCreateResponse | null>(null);
  const [copied, setCopied] = useState(false);
  const [showSecret, setShowSecret] = useState(false);

  // Select all events by default
  useEffect(() => {
    const allEvents = new Set<string>();
    for (const events of Object.values(eventCategories)) {
      for (const evt of events) allEvents.add(evt.type);
    }
    setSelectedEvents(allEvents);
  }, [eventCategories]);

  const toggleEvent = (eventType: string) => {
    setSelectedEvents((prev) => {
      const next = new Set(prev);
      next.has(eventType) ? next.delete(eventType) : next.add(eventType);
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
      const result = await onCreate(trimmedName, trimmedUrl, Array.from(selectedEvents));
      setCreatedEndpoint(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create endpoint");
    } finally {
      setCreating(false);
    }
  };

  // ── Secret display ─────────────────────────────────────────────────────────
  if (createdEndpoint) {
    return (
      <>
        <div className="fixed inset-0 z-50 bg-black/50 animate-fade-in" onClick={onClose} />
        <div className="fixed left-1/2 top-1/2 z-50 w-full max-w-lg -translate-x-1/2 -translate-y-1/2 rounded-lg border border-surface-800 bg-surface-900 p-6 shadow-xl shadow-black/40 animate-slide-up">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold text-[#F2F2F2]">Endpoint Created</h2>
            <button onClick={onClose} className="text-surface-400 hover:text-white p-1">
              <X size={18} />
            </button>
          </div>
          <p className="text-sm text-surface-400 mb-4">
            Copy the signing secret now. You won&apos;t be able to see it again.
          </p>
          <div className="text-sm text-surface-300 mb-4 space-y-1">
            <p><span className="text-surface-500">Name:</span> {createdEndpoint.name}</p>
            <p className="truncate"><span className="text-surface-500">URL:</span> {createdEndpoint.url}</p>
          </div>
          <div className="bg-surface-950 border border-surface-700 font-mono text-sm p-4 rounded relative">
            <code className="text-accent-300 break-all text-xs">
              {showSecret ? createdEndpoint.secret : "••••••••••••••••••••••••••••••••••••••••"}
            </code>
            <div className="absolute top-2 right-2 flex gap-1">
              <button
                onClick={() => setShowSecret((p) => !p)}
                className="text-surface-400 hover:text-white p-1.5 rounded text-xs"
                title={showSecret ? "Hide secret" : "Show secret"}
              >
                {showSecret ? <EyeOff size={14} /> : <Eye size={14} />}
              </button>
              <button
                onClick={async () => {
                  try {
                    await navigator.clipboard.writeText(createdEndpoint.secret);
                    setCopied(true);
                    setTimeout(() => setCopied(false), 2000);
                  } catch {}
                }}
                className={cn("text-xs p-1.5 rounded", copied ? "text-success" : "text-surface-400 hover:text-white")}
              >
                {copied ? <CheckCircle size={14} /> : <Copy size={14} />}
              </button>
            </div>
          </div>
          <p className="text-xs text-surface-500 mt-3">
            Secret format: <span className="font-mono text-surface-300">whsec_...</span>
          </p>
          <div className="flex justify-end mt-4">
            <Button variant="primary" size="sm" onClick={onClose}>Done</Button>
          </div>
        </div>
      </>
    );
  }

  // ── Create form ───────────────────────────────────────────────────────────
  const categories = Object.entries(eventCategories);

  return (
    <>
      <div className="fixed inset-0 z-50 bg-black/50 animate-fade-in" onClick={onClose} />
      <div
        className="fixed left-1/2 top-1/2 z-50 w-full max-w-2xl -translate-x-1/2 -translate-y-1/2 rounded-lg border border-surface-800 bg-surface-900 p-6 shadow-xl shadow-black/40 animate-slide-up max-h-[90vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-[#F2F2F2]">Create Webhook Endpoint</h2>
          <button onClick={onClose} className="text-surface-400 hover:text-white p-1">
            <X size={18} />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
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
            <p className="text-xs text-surface-500 mt-1">HTTPS URL that receives POST requests with webhook payloads.</p>
          </div>

          <div>
            <label className="block text-sm font-medium text-surface-300 mb-2">Subscribed Events</label>
            <p className="text-xs text-surface-500 mb-3">
              All events are selected by default. Uncheck events you don&apos;t want to receive.
            </p>
            <div className="space-y-3 max-h-64 overflow-y-auto pr-1">
              {categories.map(([category, events]) => {
                const allSelected = events.every((e) => selectedEvents.has(e.type));
                return (
                  <div key={category} className="border border-surface-800 rounded-md p-3">
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
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-1.5 ml-5">
                      {events.map((evt) => (
                        <label key={evt.type} className="flex items-center gap-2 cursor-pointer py-0.5">
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
            <Button variant="secondary" size="sm" onClick={onClose} disabled={creating}>Cancel</Button>
            <Button variant="primary" size="sm" type="submit" loading={creating}>
              Create Endpoint
            </Button>
          </div>
        </form>
      </div>
    </>
  );
}

// ─── Main Page ─────────────────────────────────────────────────────────────────

export default function WebhooksPage() {
  const [endpoints, setEndpoints] = useState<WebhookEndpoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<WebhookEndpoint | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [eventCategories, setEventCategories] = useState<EventCategories>({});
  const [eventsLoading, setEventsLoading] = useState(true);

  // ── Fetch event types ────────────────────────────────────────────────────

  const fetchEvents = useCallback(async () => {
    setEventsLoading(true);
    try {
      const data = await get<{ data: EventCategories }>("/v1/admin/webhooks/events");
      setEventCategories(data.data ?? {});
    } catch {
      setEventCategories(FALLBACK_EVENTS);
    } finally {
      setEventsLoading(false);
    }
  }, []);

  // ── Fetch endpoints ──────────────────────────────────────────────────────

  const fetchEndpoints = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await get<{ data: WebhookEndpoint[] }>("/v1/admin/webhooks");
      setEndpoints(data.data ?? []);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load webhooks");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchEvents();
    fetchEndpoints();
  }, [fetchEvents, fetchEndpoints]);

  // ── Create ───────────────────────────────────────────────────────────────

  const handleCreate = async (name: string, url: string, events: string[]): Promise<WebhookCreateResponse> => {
    const result = await post<WebhookCreateResponse>("/v1/admin/webhooks", { name, url, events });
    toast.success("Webhook endpoint created");
    return result;
  };

  const handleCloseCreate = () => {
    setShowCreate(false);
    fetchEndpoints();
  };

  // ── Delete ───────────────────────────────────────────────────────────────

  const handleDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await del(`/v1/admin/webhooks/${deleteTarget.id}`);
      setEndpoints((prev) => prev.filter((e) => e.id !== deleteTarget.id));
      setDeleteTarget(null);
      toast.success("Webhook endpoint deleted");
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Failed to delete webhook";
      setError(msg);
      toast.error(msg);
    } finally {
      setDeleting(false);
    }
  };

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
    <RequireAuth>
    <div className="space-y-6">
      <PageHeader
        title="Webhooks"
        description="Configure HTTP endpoints to receive real-time events from your OpenZep instance"
        actions={
          <Button variant="primary" size="sm" icon={<Plus size={14} />} onClick={() => setShowCreate(true)} disabled={eventsLoading}>
            Create Endpoint
          </Button>
        }
      />

      {/* Error */}
      {error && <ErrorState message={error} onRetry={fetchEndpoints} />}

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
              {loading ? (
                <TableSkeleton rows={3} cols={7} colWidths={["w-32", "w-40", "w-28", "w-16", "w-20", "w-24", "w-16"]} />
              ) : endpoints.length === 0 ? (
                <tr>
                  <td colSpan={7}>
                    <EmptyState
                      icon={Webhook}
                      title="No webhook endpoints configured"
                      description="Create an endpoint to start receiving webhook events"
                      action={<Button variant="primary" size="sm" icon={<Plus size={14} />} onClick={() => setShowCreate(true)}>Create Endpoint</Button>}
                    />
                  </td>
                </tr>
              ) : (
                endpoints.map((ep, idx) => (
                  <tr
                    key={ep.id}
                    className={cn(
                      "transition-colors hover:bg-surface-800/50",
                      idx % 2 === 0 ? "bg-surface-950/50" : "",
                      !ep.is_active && "opacity-50",
                    )}
                  >
                    <td className="px-4 py-3">
                      <span className={cn("font-medium", ep.is_active ? "text-white" : "text-surface-500")}>{ep.name}</span>
                    </td>
                    <td className="px-4 py-3 max-w-[200px]">
                      <span className="font-mono text-xs text-surface-400 truncate block" title={ep.url}>
                        {truncateUrl(ep.url)}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex flex-wrap gap-1 max-w-[160px]">
                        {ep.events.length > 0 ? (
                          ep.events.slice(0, 3).map((evt) => (
                            <Badge key={evt} variant="brand" size="sm">
                              {evt.split(".").pop()}
                            </Badge>
                          ))
                        ) : (
                          <span className="text-surface-500 text-xs italic">All</span>
                        )}
                        {ep.events.length > 3 && (
                          <span className="text-[10px] text-surface-500 font-mono">+{ep.events.length - 3}</span>
                        )}
                      </div>
                    </td>
                    <td className="px-4 py-3 text-center">
                      <Badge variant={ep.is_active ? "success" : "default"} size="sm">
                        {ep.is_active ? "Active" : "Disabled"}
                      </Badge>
                    </td>
                    <td className="px-4 py-3 text-surface-400 text-xs">{timeAgo(ep.last_delivery_at)}</td>
                    <td className="px-4 py-3">
                      <span className="text-surface-300 text-xs">{formatDate(ep.created_at)}</span>
                    </td>
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
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* ── Create Dialog ─────────────────────────────────────────────────────── */}
      {showCreate && !eventsLoading && (
        <CreateDialog
          eventCategories={eventCategories}
          onClose={handleCloseCreate}
          onCreate={handleCreate}
        />
      )}

      {/* ── Delete Confirm Dialog ─────────────────────────────────────────────── */}
      <ConfirmDialog
        open={!!deleteTarget}
        title="Delete Webhook"
        message={`Are you sure you want to delete "${deleteTarget?.name}"? Any services using this webhook URL will immediately stop receiving events.`}
        confirmLabel="Delete"
        variant="danger"
        loading={deleting}
        onConfirm={handleDelete}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  </RequireAuth>
  );
}
