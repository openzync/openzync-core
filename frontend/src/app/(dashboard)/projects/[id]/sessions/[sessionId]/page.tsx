"use client";
import { RequireAuth } from "../../../../require-auth";

import { useState, useEffect } from "react";
import { useParams, usePathname, useRouter } from "next/navigation";
import Link from "next/link";
import {
  ArrowLeft,
  Copy,
  Check,
  Calendar,
  Clock,
  MessageSquare,
  Database,
  Hash,
  User as UserIcon,
  ExternalLink,
} from "lucide-react";
import { get, ApiError } from "@/lib/api-client";
import { smartTimestamp, truncateId, copyToClipboard } from "@/lib/utils";
import { useProject } from "@/stores/project-context";
import { ErrorState } from "@/components/shared/error-state";
import { Badge } from "@/components/ui/badge";

// ─── Types ─────────────────────────────────────────────────────────────────────

interface SessionDetail {
  id: string;
  user_id: string;
  external_id: string;
  is_active: boolean;
  message_count: number;
  fact_count: number;
  created_at: string;
  closed_at?: string | null;
}

// ─── Tab configuration ─────────────────────────────────────────────────────────

interface Tab {
  label: string;
  path: string;
}

const TABS: Tab[] = [
  { label: "Messages", path: "messages" },
  { label: "Facts", path: "facts" },
  { label: "Graph", path: "graph" },
  { label: "Classifications", path: "classifications" },
  { label: "Extractions", path: "extractions" },
];

// ─── Copy Button ───────────────────────────────────────────────────────────────

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);

  async function handleCopy() {
    const ok = await copyToClipboard(text);
    if (ok) {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
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

// ─── Page ──────────────────────────────────────────────────────────────────────

export default function SessionDetailPage() {
  const params = useParams();
  const pathname = usePathname();
  const router = useRouter();

  const sessionId = params.sessionId as string;
  const { project, loading: projectLoading } = useProject();
  const projectId = project?.id;

  const [session, setSession] = useState<SessionDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // Determine active tab from current path
  const activeTab = (() => {
    for (const tab of TABS) {
      if (pathname.endsWith(`/${tab.path}`)) return tab.path;
    }
    return null;
  })();

  // Fetch session
  useEffect(() => {
    if (!sessionId || !projectId) {
      setLoading(false);
      setError(!projectId ? "No project selected." : "No session ID provided.");
      return;
    }

    async function fetchSession() {
      setLoading(true);
      setError("");

      try {
        const data = await get<SessionDetail>(
          `/v1/projects/${projectId}/sessions/${sessionId}`,
        );
        setSession(data);
      } catch (err) {
        if (err instanceof ApiError && err.isNotFound) {
          setError("Session not found.");
        } else {
          setError(err instanceof ApiError ? err.message : "Network error. Please try again.");
        }
      } finally {
        setLoading(false);
      }
    }

    fetchSession();
  }, [sessionId, projectId]);

  // Build tab href
  function tabHref(tab: Tab): string {
    return `/projects/${projectId}/sessions/${sessionId}/${tab.path}`;
  }

  // Breadcrumb
  function Breadcrumb() {
    return (
      <nav className="flex items-center gap-2 text-sm text-surface-400 mb-4">
        <Link
          href={`/projects/${projectId}/sessions`}
          className="hover:text-surface-200 transition-colors"
        >
          Sessions
        </Link>
        <span className="text-surface-600">/</span>
        <span className="text-surface-100 font-medium">
          {loading
            ? "…"
            : session?.external_id ?? sessionId.slice(0, 8)}
        </span>
      </nav>
    );
  }

  // Loading guard
  if (projectLoading) {
    return (
      <RequireAuth>
        <div className="space-y-6">
          <div className="h-6 w-48 rounded bg-surface-800 animate-pulse" />
        </div>
      </RequireAuth>
    );
  }

  // Render
  return (
    <RequireAuth>
    <div className="space-y-6">
      {/* Back button */}
      <button
        onClick={() => router.push(`/projects/${projectId}/sessions`)}
        className="btn-ghost text-xs -ml-2"
      >
        <ArrowLeft size={14} />
        Back to Sessions
      </button>

      {/* Breadcrumb */}
      {projectId && <Breadcrumb />}

      {/* Metadata card */}
      <div className="card-base p-6">
        {loading ? (
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
          </div>
        ) : error ? (
          <ErrorState message={error} onRetry={() => window.location.reload()} />
        ) : session ? (
          <>
            <div className="flex items-start justify-between mb-6">
              <div>
                <h1 className="text-xl font-bold tracking-tight">
                  {session.external_id}
                </h1>
                <p className="text-xs text-surface-500 mt-0.5">
                  Session overview
                </p>
              </div>
              <Badge variant={session.is_active ? "success" : "default"} size="sm">
                <span className={`mr-1.5 h-1.5 w-1.5 rounded-full ${session.is_active ? "bg-success" : "bg-surface-500"}`} />
                {session.is_active ? "Active" : "Closed"}
              </Badge>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
              <MetadataRow icon={<Hash size={16} />} label="Session ID">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-xs bg-surface-800 rounded px-2 py-0.5">
                    {truncateId(session.id)}
                  </span>
                  <CopyButton text={session.id} />
                </div>
              </MetadataRow>

              <MetadataRow icon={<UserIcon size={16} />} label="Created By">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-xs bg-surface-800 rounded px-2 py-0.5">
                    {truncateId(session.user_id)}
                  </span>
                  <CopyButton text={session.user_id} />
                </div>
              </MetadataRow>

              <MetadataRow icon={<Calendar size={16} />} label="Created">
                <span>{smartTimestamp(session.created_at)}</span>
              </MetadataRow>

              <MetadataRow icon={<Clock size={16} />} label="Closed">
                {session.closed_at ? (
                  <span>{smartTimestamp(session.closed_at)}</span>
                ) : (
                  <span className="text-surface-500">—</span>
                )}
              </MetadataRow>

              <MetadataRow icon={<MessageSquare size={16} />} label="Messages">
                <span className="font-semibold">
                  {session.message_count.toLocaleString()}
                </span>
              </MetadataRow>

              <MetadataRow icon={<Database size={16} />} label="Facts">
                <span className="font-semibold">
                  {session.fact_count.toLocaleString()}
                </span>
              </MetadataRow>
            </div>
          </>
        ) : null}
      </div>

      {/* Tab navigation */}
      {session && (
        <div className="border-b border-surface-800">
          <nav className="flex gap-6 -mb-px">
            {TABS.map((tab) => {
              const isActive = activeTab === tab.path;
              return (
                <Link
                  key={tab.path}
                  href={tabHref(tab)}
                  className={`pb-3 text-sm font-medium border-b-2 transition-colors ${
                    isActive
                      ? "text-brand-500 border-brand-500"
                      : "text-surface-400 border-transparent hover:text-surface-200 hover:border-surface-600"
                  }`}
                >
                  {tab.label}
                </Link>
              );
            })}
          </nav>
        </div>
      )}

      {/* Tab content placeholder */}
      {session && !activeTab && (
        <div className="card-base p-6 flex flex-col items-center justify-center py-12 text-surface-500">
          <ExternalLink size={32} className="mb-3 text-surface-600" />
          <p className="text-sm">Select a tab above to view session data.</p>
          <p className="text-xs mt-1">
            Messages, facts, graph, classifications, and extractions.
          </p>
        </div>
      )}
    </div>
    </RequireAuth>
  );
}
