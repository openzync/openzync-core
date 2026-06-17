"use client";
import { RequireAuth } from "../../../require-auth";

import { useEffect, useRef, useState, useCallback } from "react";
import { useParams, useSearchParams } from "next/navigation";
import { ForceGraph, type GraphNodeData, type GraphEdgeData } from "@/components/force-graph";
import { get, API_BASE, getAccessToken, ApiError } from "@/lib/api-client";
import SessionTabs from "../tabs";

interface NodesResponse {
  data: { items: GraphNodeData[] };
}

interface EdgesResponse {
  data: { items: GraphEdgeData[] };
}

function buildAuthHeaders(): Record<string, string> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const token = getAccessToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  return headers;
}

export default function SessionGraphPage() {
  const params = useParams();
  const searchParams = useSearchParams();
  const sessionId = params.id as string;
  const userId = searchParams.get("userId") ?? "";
  const loadAttempted = useRef(false);

  const [graphData, setGraphData] = useState<{ nodes: GraphNodeData[]; edges: GraphEdgeData[] } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    if (!userId || !sessionId) return;
    setLoading(true);
    setError(null);
    loadAttempted.current = true;

    try {
      // Step 1: fetch session-scoped nodes
      const nodeData = await get<NodesResponse>(
        `/v1/users/${userId}/graph/nodes?limit=200&session_id=${sessionId}`,
      );

      const items: GraphNodeData[] = nodeData.data?.items ?? [];

      if (items.length === 0) {
        setGraphData({ nodes: [], edges: [] });
        setLoading(false);
        return;
      }

      // Step 2: fetch edges for each node in batches of 5
      const allEdges: GraphEdgeData[] = [];
      const seen = new Set<string>();

      for (let i = 0; i < items.length; i += 5) {
        const batch = items.slice(i, i + 5);
        const batchResults = await Promise.allSettled(
          batch.map((node) =>
            get<EdgesResponse>(
              `/v1/users/${userId}/graph/edges?subject_id=${node.id}&limit=50`,
            ).then((d) => d.data?.items ?? []),
          ),
        );

        for (const result of batchResults) {
          if (result.status === "fulfilled") {
            for (const edge of result.value) {
              if (edge.source_id === edge.target_id) continue;
              const key = [edge.source_id, edge.target_id].sort().join("::");
              if (seen.has(key)) continue;
              seen.add(key);
              allEdges.push(edge);
            }
          }
        }
      }

      setGraphData({ nodes: items, edges: allEdges });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "An unknown error occurred");
    } finally {
      setLoading(false);
    }
  }, [userId, sessionId]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  // ── Missing userId guard ─────────────────────────────────────────────
  if (!userId) {
    return (
      <RequireAuth>
      <div>
        <SessionTabs sessionId={sessionId} userId="" activeTab="graph" />
        <div className="card-base p-8 flex flex-col items-center justify-center gap-3 text-surface-500 mt-4">
          <p className="text-sm">No user selected. Provide a <code className="text-surface-300 font-mono text-xs bg-surface-800 px-1.5 py-0.5 rounded">userId</code> query parameter.</p>
        </div>
      </div>
      </RequireAuth>
    );
  }

  return (
    <RequireAuth>
    <div>
      <SessionTabs sessionId={sessionId} userId={userId} activeTab="graph" />
      <ForceGraph
        nodes={graphData?.nodes ?? []}
        edges={graphData?.edges ?? []}
        loading={loading}
        error={error}
        onRetry={loadData}
        apiConfig={{
          baseUrl: API_BASE,
          userId,
          headers: buildAuthHeaders(),
        }}
        showFilter
        showControls
        showLegend
        emptyMessage="No entities found for this session. Facts must be extracted first."
      />
    </div>
    </RequireAuth>
  );
}
