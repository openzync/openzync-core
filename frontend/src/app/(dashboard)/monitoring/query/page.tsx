"use client";

import { useState } from "react";
import { Play, AlertCircle } from "lucide-react";

export default function PromQueryPage() {
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const handleQuery = async () => {
    if (!query.trim()) return;
    setLoading(true);
    setError("");
    setResult(null);
    try {
      const token = sessionStorage.getItem("mg_access_token");
      const headers: Record<string, string> = {};
      if (token) headers["Authorization"] = `Bearer ${token}`;
      const res = await fetch(`http://localhost:8000/metrics/query?query=${encodeURIComponent(query.trim())}`, { headers });
      if (!res.ok) throw new Error("Query failed");
      const json = await res.json();
      setResult(json);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Prometheus Query</h1>
        <p className="text-sm text-surface-400 mt-1">Run ad-hoc PromQL queries</p>
      </div>
      <div className="card-base p-4">
        <div className="flex gap-2">
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleQuery()}
            placeholder='e.g. rate(http_requests_total[5m])'
            className="input-base flex-1 font-mono text-sm"
          />
          <button onClick={handleQuery} disabled={loading || !query.trim()} className="btn-primary">
            <Play size={16} />
            Run
          </button>
        </div>
      </div>
      {loading && <div className="card-base p-6 text-center text-surface-400 text-sm">Running query...</div>}
      {error && <div className="card-base p-4 flex items-center gap-3 text-error text-sm"><AlertCircle size={18} />{error}</div>}
      {result && (
        <div className="card-base p-4">
          <pre className="text-sm font-mono text-surface-300 whitespace-pre-wrap bg-surface-950 p-4 rounded border border-surface-800 overflow-auto max-h-96">
            {JSON.stringify(result, null, 2)}
          </pre>
        </div>
      )}
    </div>
  );
}
