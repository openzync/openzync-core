"use client";

import { useParams, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { FileText, AlertCircle } from "lucide-react";
import SessionTabs from "../tabs";

interface FactRow {
  id: string;
  content: string;
  subject: string | null;
  predicate: string | null;
  object: string | null;
  confidence: number;
  created_at: string;
}

function formatDate(raw: string): string {
  try { return new Date(raw).toLocaleDateString(); }
  catch { return raw; }
}

function confidenceColor(score: number): string {
  if (score >= 0.8) return "text-success";
  if (score >= 0.5) return "text-warning";
  return "text-error";
}

function confidenceBg(score: number): string {
  if (score >= 0.8) return "bg-success/10";
  if (score >= 0.5) return "bg-warning/10";
  return "bg-error/10";
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
        const token = sessionStorage.getItem("mg_access_token");
        const headers: Record<string, string> = {};
        if (token) headers["Authorization"] = `Bearer ${token}`;

        const res = await fetch(
          `http://localhost:8000/v1/users/${userId}/sessions/${sessionId}/facts?limit=50`,
          { headers },
        );
        if (!res.ok) throw new Error("Failed to load facts");
        const data = await res.json();
        setFacts((data.data ?? []) as FactRow[]);
      } catch (err: any) {
        setError(err.message ?? "An error occurred");
      } finally {
        setLoading(false);
      }
    }
    fetchFacts();
  }, [userId, sessionId]);

  return (
    <div>
      <SessionTabs sessionId={sessionId} userId={userId} activeTab="facts" />

      {loading ? (
        <div className="card-base p-6 space-y-3">
          {[1,2,3].map(i => <div key={i} className="h-12 bg-surface-800 animate-pulse rounded" />)}
        </div>
      ) : error ? (
        <div className="card-base p-6 flex items-center gap-3 text-error">
          <AlertCircle size={18} />
          <span className="text-sm">{error}</span>
        </div>
      ) : facts.length === 0 ? (
        <div className="card-base p-6 text-center text-surface-500 text-sm">
          <FileText size={32} className="mx-auto mb-3 opacity-50" />
          No facts extracted from this session yet. Facts appear after messages are processed by the extraction worker.
        </div>
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
                    <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${confidenceBg(fact.confidence)} ${confidenceColor(fact.confidence)}`}>
                      {(fact.confidence * 100).toFixed(0)}%
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right text-sm text-surface-400">{formatDate(fact.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
