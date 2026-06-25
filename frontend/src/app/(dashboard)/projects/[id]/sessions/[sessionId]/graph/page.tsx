"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import { useParams } from "next/navigation";
import { ForceGraph, type GraphNodeData, type GraphEdgeData } from "@/components/force-graph";
import { get, API_BASE, getAccessToken, ApiError } from "@/lib/api-client";
import { useProject } from "@/stores/project-context";
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
  const sessionId = params.sessionId as string;
  const { project } = useProject();
  const projectId = project?.id;
  const loadAttempted = useRef(false);

  const [graphData, setGraphData] = useState<{ nodes: GraphNodeData[]; edges: GraphEdgeData[] } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    if (!projectId || !sessionId) return;
    setLoading(true);
    setError(null);
    loadAttempted.current = true;

    try {
      // Step 1: fetch session-scoped nodes
      const nodeData = await get<NodesResponse>(
        `/v1/projects/${projectId}/graph/nodes?limit=200&session_id=${sessionId}`,
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
              `/v1/projects/${projectId}/graph/edges?subject_id=${node.id}&limit=50`,
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
  }, [projectId, sessionId]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  if (!projectId) {
    return (
      <div>
        <SessionTabs sessionId={sessionId} activeTab="graph" />
        <div className="card-base p-8 flex flex-col items-center justify-center gap-3 text-surface-500 mt-4">
          <p className="text-sm">No project selected.</p>
        </div>
      </div>
    );
  }

  return (
    <div>
      <SessionTabs sessionId={sessionId} activeTab="graph" />
      <ForceGraph
        nodes={graphData?.nodes ?? []}
        edges={graphData?.edges ?? []}
        loading={loading}
        error={error}
        onRetry={loadData}
        apiConfig={{
          baseUrl: API_BASE,
          projectId,
          headers: buildAuthHeaders(),
        }}
        showFilter
        showControls
        showLegend
        emptyMessage="No entities found for this session. Facts must be extracted first."
      />
    </div>
  );
}
