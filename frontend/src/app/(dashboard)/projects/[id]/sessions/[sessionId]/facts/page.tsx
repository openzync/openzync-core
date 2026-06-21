"use client";
import { RequireAuth } from "../../../../../require-auth";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { FileText } from "lucide-react";
import { get, ApiError } from "@/lib/api-client";
import { formatDate } from "@/lib/utils";
import { useProject } from "@/stores/project-context";
import SessionTabs from "../tabs";
import { EmptyState } from "@/components/shared/empty-state";
import { ErrorState } from "@/components/shared/error-state";
import { Badge } from "@/components/ui/badge";
import { TableSkeleton } from "@/components/shared/skeleton";

interface FactRow {
  id: string;
  content: string;
  subject: string | null;
  predicate: string | null;
  object: string | null;
  confidence: number;
  created_at: string;
}

function confidenceVariant(score: number): "success" | "warning" | "error" {
  if (score >= 0.8) return "success";
  if (score >= 0.5) return "warning";
  return "error";
}

export default function SessionFactsPage() {
  const params = useParams();
  const sessionId = params.sessionId as string;
  const { project } = useProject();
  const projectId = project?.id;

  const [facts, setFacts] = useState<FactRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!projectId) return;
    async function fetchFacts() {
      setLoading(true);
      setError("");
      try {
        const json = await get<{ data: FactRow[] }>(
          `/v1/projects/${projectId}/sessions/${sessionId}/facts?limit=50`,
        );
        setFacts(json.data ?? []);
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "Failed to load facts");
      } finally {
        setLoading(false);
      }
    }
    fetchFacts();
  }, [projectId, sessionId]);

  return (
    <RequireAuth>
    <div>
      <SessionTabs sessionId={sessionId} activeTab="facts" />
      {loading ? (
        <TableSkeleton rows={5} cols={3} colWidths={["w-48", "w-32", "w-16"]} />
      ) : error ? (
        <ErrorState message={error} onRetry={() => window.location.reload()} />
      ) : facts.length === 0 ? (
        <EmptyState icon={FileText} title="No facts extracted yet"
          description="Facts will appear here once the session is processed." />
      ) : (
        <div className="card-base overflow-hidden">
          <table className="w-full">
            <thead className="bg-surface-800 text-surface-400 text-xs uppercase tracking-wider">
              <tr>
                <th className="px-4 py-3 text-left">Content</th>
                <th className="px-4 py-3 text-left">Triple</th>
                <th className="px-4 py-3 text-center">Confidence</th>
                <th className="px-4 py-3 text-right">Extracted</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-surface-800">
              {facts.map((fact) => (
                <tr key={fact.id} className="even:bg-surface-950/50">
                  <td className="px-4 py-3 text-sm text-surface-200 max-w-xs truncate">{fact.content}</td>
                  <td className="px-4 py-3 text-sm text-surface-400">
                    {fact.subject && fact.predicate && fact.object
                      ? `${fact.subject} → ${fact.predicate} → ${fact.object}`
                      : "—"}
                  </td>
                  <td className="px-4 py-3 text-center">
                    <Badge variant={confidenceVariant(fact.confidence)} size="sm">
                      {(fact.confidence * 100).toFixed(0)}%
                    </Badge>
                  </td>
                  <td className="px-4 py-3 text-right text-sm text-surface-400 whitespace-nowrap">
                    {formatDate(fact.created_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
    </RequireAuth>
  );
}
