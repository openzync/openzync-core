"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { Database } from "lucide-react";
import { get, ApiError } from "@/lib/api-client";
import { useProject } from "@/stores/project-context";
import SessionTabs from "../tabs";
import { EmptyState } from "@/components/shared/empty-state";
import { ErrorState } from "@/components/shared/error-state";
import { TableSkeleton } from "@/components/shared/skeleton";

interface Extraction {
  id: string;
  schema_id: string | null;
  data: Record<string, unknown>;
  created_at: string;
}

export default function SessionExtractionsPage() {
  const params = useParams();
  const sessionId = params.sessionId as string;
  const { project } = useProject();
  const projectId = project?.id;

  const [data, setData] = useState<Extraction[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!projectId) return;
    async function loadData() {
      setLoading(true);
      setError("");
      try {
        const json = await get<{ items: Extraction[] }>(
          `/v1/projects/${projectId}/sessions/${sessionId}/structured-extractions`,
        );
        setData(json.items ?? []);
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "Failed to load extractions");
      } finally {
        setLoading(false);
      }
    }
    loadData();
  }, [projectId, sessionId]);

  return (
    <div>
      <SessionTabs sessionId={sessionId} activeTab="extractions" />
      {loading ? (
        <TableSkeleton rows={2} cols={1} colWidths={["w-full"]} />
      ) : error ? (
        <ErrorState message={error} onRetry={() => window.location.reload()} />
      ) : data.length === 0 ? (
        <EmptyState
          icon={Database}
          title="No structured extractions available yet"
          description="Extractions will appear here once data is processed."
        />
      ) : (
        <div className="space-y-3">
          {data.map((ext) => (
            <div key={ext.id} className="card-base p-4">
              <div className="text-xs text-surface-500 mb-2 font-mono">Schema: {ext.schema_id ?? "none"}</div>
              <pre className="text-sm font-mono text-surface-300 whitespace-pre-wrap bg-surface-950 p-3 rounded border border-surface-800 overflow-auto max-h-48">
                {JSON.stringify(ext.data, null, 2)}
              </pre>
              <div className="text-xs text-surface-600 mt-2">{new Date(ext.created_at).toLocaleString()}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
