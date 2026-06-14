"use client";

import { useParams, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { Database, AlertCircle } from "lucide-react";
import SessionTabs from "../tabs";

interface Extraction {
  id: string;
  schema_id: string | null;
  data: Record<string, unknown>;
  created_at: string;
}

export default function SessionExtractionsPage() {
  const params = useParams();
  const searchParams = useSearchParams();
  const sessionId = params.id as string;
  const userId = searchParams.get("userId") ?? "";

  const [data, setData] = useState<Extraction[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!userId) return;
    async function loadData() {
      setLoading(true);
      setError("");
      try {
        const token = sessionStorage.getItem("mg_access_token");
        const headers: Record<string, string> = {};
        if (token) headers["Authorization"] = `Bearer ${token}`;
        const res = await fetch(`http://localhost:8000/v1/users/${userId}/sessions/${sessionId}/structured-extractions`, { headers });
        if (!res.ok) throw new Error("Failed to load extractions");
        const json = await res.json();
        setData(json.items ?? []);
      } catch (err: any) {
        setError(err.message);
      } finally {
        setLoading(false);
      }
    }
    loadData();
  }, [userId, sessionId]);

  return (
    <div>
      <SessionTabs sessionId={sessionId} userId={userId} activeTab="extractions" />
      {loading ? (
        <div className="card-base p-6 space-y-3">
          {[1,2].map(i => <div key={i} className="h-24 bg-surface-800 animate-pulse rounded" />)}
        </div>
      ) : error ? (
        <div className="card-base p-6 flex items-center gap-3 text-error text-sm"><AlertCircle size={18} />{error}</div>
      ) : data.length === 0 ? (
        <div className="card-base p-6 text-center text-surface-500 text-sm">
          <Database size={32} className="mx-auto mb-3 opacity-50" />
          No structured extractions available yet.
        </div>
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
