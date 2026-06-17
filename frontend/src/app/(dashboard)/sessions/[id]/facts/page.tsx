"use client";
import { RequireAuth } from "../../../require-auth";

import { useParams, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { FileText } from "lucide-react";
import { get, ApiError } from "@/lib/api-client";
import { formatDate } from "@/lib/utils";
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
  const searchParams = useSearchParams();
  const sessionId = params.id as string;
  const userId = searchParams.get("userId") ?? "";

  const [facts, setFacts] = useState<FactRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!userId) return;
    async function fetchFacts() {
      setLoading(true);
      setError("");
      try {
        const json = await get<{ data: FactRow[] }>(
          `/v1/users/${userId}/sessions/${sessionId}/facts?limit=50`,
        );
        setFacts(json.data ?? []);
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "Failed to load facts");
      } finally {
        setLoading(false);
      }
    }
    fetchFacts();
  }, [userId, sessionId]);

  return (
    <RequireAuth>
    <div>
      <SessionTabs sessionId={sessionId} userId={userId} activeTab="facts" />

      {loading ? (
        <TableSkeleton rows={3} cols={4} colWidths={["w-48", "w-40", "w-16", "w-24"]} />
      ) : error ? (
        <ErrorState message={error} onRetry={() => window.location.reload()} />
      ) : facts.length === 0 ? (
        <EmptyState
          icon={FileText}
          title="No facts extracted from this session yet"
          description="Facts appear after messages are processed by the extraction worker."
        />
      ) : (
        <div className="card-base overflow-hidden">
          <table className="w-full">
            <thead className="bg-surface-800 text-surface-400 text-xs uppercase tracking-wider">
              <tr>
                <th className="px-4 py-3 text-left">Fact</th>
                <th className="px-4 py-3 text-left">Triple</th>
                <th className="px-4 py-3 text-center">Confidence</th>
                <th className="px-4 py-3 text-right">Extracted</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-surface-800">
              {facts.map((fact) => (
                <tr key={fact.id} className="even:bg-surface-950/50 hover:bg-surface-800/50">
                  <td className="px-4 py-3 text-sm">{fact.content}</td>
                  <td className="px-4 py-3 text-sm text-surface-400 font-mono text-xs">
                    {fact.subject ?? "?"} → {fact.predicate ?? "?"} → {fact.object ?? "?"}
                  </td>
                  <td className="px-4 py-3 text-center">
                    <Badge variant={confidenceVariant(fact.confidence)}>
                      {(fact.confidence * 100).toFixed(0)}%
                    </Badge>
                  </td>
                  <td className="px-4 py-3 text-right text-sm text-surface-400">
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
