"use client";

import { useState } from "react";
import { Play } from "lucide-react";
import { get, ApiError } from "@/lib/api-client";
import { PageHeader } from "@/components/shared/page-header";
import { ErrorState } from "@/components/shared/error-state";
import { Button } from "@/components/ui/button";

export default function PromQueryPage() {
  const [query, setQuery] = useState("");
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [result, setResult] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const handleQuery = async () => {
    if (!query.trim()) return;
    setLoading(true); setError(""); setResult(null);
    try {
      const json = await get<unknown>(`/metrics/query?query=${encodeURIComponent(query.trim())}`);
      setResult(json);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Query failed");
    } finally { setLoading(false); }
  };

  return (
    <div className="space-y-6">
      <PageHeader title="Prometheus Query" description="Run ad-hoc PromQL queries" />
      <div className="card-base p-4">
        <div className="flex gap-2">
          <input type="text" value={query} onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleQuery()}
            placeholder="e.g. rate(http_requests_total[5m])" className="input-base flex-1 font-mono text-sm" />
          <Button variant="primary" onClick={handleQuery} loading={loading} disabled={!query.trim()} icon={<Play size={16} />}>Run</Button>
        </div>
      </div>
      {error && <ErrorState message={error} onRetry={handleQuery} />}
      {result && (
        <div className="card-base p-4">
          <pre className="text-sm font-mono text-surface-300 whitespace-pre-wrap bg-surface-950 p-4 rounded border border-surface-800 overflow-auto max-h-96">
            {JSON.stringify(result as Record<string, unknown>, null, 2)}
          </pre>
        </div>
      )}
    </div>
  );
}
