"use client";

import { useParams, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { Tags, AlertCircle } from "lucide-react";
import SessionTabs from "../tabs";

interface Classification {
  id: string;
  intent: string;
  emotion: string;
  confidence: number;
  created_at: string;
}

export default function SessionClassificationsPage() {
  const params = useParams();
  const searchParams = useSearchParams();
  const sessionId = params.id as string;
  const userId = searchParams.get("userId") ?? "";

  const [data, setData] = useState<Classification[]>([]);
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
        const res = await fetch(`http://localhost:8000/v1/users/${userId}/sessions/${sessionId}/classifications`, { headers });
        if (!res.ok) throw new Error("Failed to load classifications");
        const json = await res.json();
        setData(json.data ?? []);
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
      <SessionTabs sessionId={sessionId} userId={userId} activeTab="classifications" />
      {loading ? (
        <div className="card-base p-6 space-y-3">
          {[1,2].map(i => <div key={i} className="h-12 bg-surface-800 animate-pulse rounded" />)}
        </div>
      ) : error ? (
        <div className="card-base p-6 flex items-center gap-3 text-error text-sm"><AlertCircle size={18} />{error}</div>
      ) : data.length === 0 ? (
        <div className="card-base p-6 text-center text-surface-500 text-sm">
          <Tags size={32} className="mx-auto mb-3 opacity-50" />
          No classifications available yet.
        </div>
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
                    <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-brand-500/10 text-brand-300">
                      {(c.confidence * 100).toFixed(0)}%
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right text-sm text-surface-400">{new Date(c.created_at).toLocaleDateString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
