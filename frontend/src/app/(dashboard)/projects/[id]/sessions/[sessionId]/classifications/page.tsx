"use client";
import { RequireAuth } from "../../../../../require-auth";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { Tags } from "lucide-react";
import { get, ApiError } from "@/lib/api-client";
import { useProject } from "@/stores/project-context";
import SessionTabs from "../tabs";
import { EmptyState } from "@/components/shared/empty-state";
import { ErrorState } from "@/components/shared/error-state";
import { Badge } from "@/components/ui/badge";
import { TableSkeleton } from "@/components/shared/skeleton";

interface Classification {
  id: string;
  intent: string;
  emotion: string;
  confidence: number;
  created_at: string;
}

export default function SessionClassificationsPage() {
  const params = useParams();
  const sessionId = params.sessionId as string;
  const { project } = useProject();
  const projectId = project?.id;

  const [data, setData] = useState<Classification[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!projectId) return;
    async function loadData() {
      setLoading(true);
      setError("");
      try {
        const json = await get<{ data: Classification[] }>(
          `/v1/projects/${projectId}/sessions/${sessionId}/classifications`,
        );
        setData(json.data ?? []);
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "Failed to load classifications");
      } finally {
        setLoading(false);
      }
    }
    loadData();
  }, [projectId, sessionId]);

  return (
    <RequireAuth>
    <div>
      <SessionTabs sessionId={sessionId} activeTab="classifications" />
      {loading ? (
        <TableSkeleton rows={3} cols={4} colWidths={["w-32", "w-24", "w-16", "w-24"]} />
      ) : error ? (
        <ErrorState message={error} onRetry={() => window.location.reload()} />
      ) : data.length === 0 ? (
        <EmptyState
          icon={Tags}
          title="No classifications available yet"
          description="Classification data will appear here once processed."
        />
      ) : (
        <div className="card-base overflow-hidden">
          <table className="w-full">
            <thead className="bg-surface-800 text-surface-400 text-xs uppercase tracking-wider">
              <tr>
                <th className="px-4 py-3 text-left">Intent</th>
                <th className="px-4 py-3 text-left">Emotion</th>
                <th className="px-4 py-3 text-center">Confidence</th>
                <th className="px-4 py-3 text-right">Created</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-surface-800">
              {data.map((c) => (
                <tr key={c.id} className="even:bg-surface-950/50">
                  <td className="px-4 py-3 text-sm">{c.intent}</td>
                  <td className="px-4 py-3 text-sm text-surface-400">{c.emotion}</td>
                  <td className="px-4 py-3 text-center">
                    <Badge variant="brand">{(c.confidence * 100).toFixed(0)}%</Badge>
                  </td>
                  <td className="px-4 py-3 text-right text-sm text-surface-400">
                    {new Date(c.created_at).toLocaleDateString()}
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
