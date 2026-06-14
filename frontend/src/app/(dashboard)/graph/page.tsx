"use client";
import { RequireAuth } from "../require-auth";

import { useEffect, useState, useCallback } from "react";
import { RotateCcw } from "lucide-react";
import { ForceGraph, type GraphNodeData, type GraphEdgeData } from "@/components/force-graph";

// ╔══════════════════════════════════════════════════════════════════════════════╗
// ║ Types                                                                       ║
// ╚══════════════════════════════════════════════════════════════════════════════╝

interface UsersApiResponse {
  data: { id: string; name: string | null }[];
}

interface NodesApiResponse {
  data: {
    items: GraphNodeData[];
    has_more: boolean;
  };
}

interface EdgesApiResponse {
  data: {
    items: GraphEdgeData[];
  };
}

// ╔══════════════════════════════════════════════════════════════════════════════╗
// ║ Constants                                                                   ║
// ╚══════════════════════════════════════════════════════════════════════════════╝

const API_BASE = "http://localhost:8000";

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (typeof window !== "undefined") {
    const token = sessionStorage.getItem("mg_access_token");
    if (token) headers["Authorization"] = `Bearer ${token}`;
  }
  return headers;
}

// ╔══════════════════════════════════════════════════════════════════════════════╗
// ║ Page                                                                        ║
// ╚══════════════════════════════════════════════════════════════════════════════╝

export default function GraphExplorerPage() {
  const [graphData, setGraphData] = useState<{ nodes: GraphNodeData[]; edges: GraphEdgeData[] } | null>(null);
  const [loading, setLoading] = useState(true);
  const [hasError, setHasError] = useState(false);
  const [userName, setUserName] = useState<string>("");
  const [userId, setUserId] = useState<string>("");

  const loadData = useCallback(async () => {
    setLoading(true);
    setHasError(false);

    try {
      // Step 1: fetch first user
      const usersRes = await fetch(`${API_BASE}/v1/users?limit=1`, {
        headers: authHeaders(),
      });
      if (!usersRes.ok) throw new Error(`Failed to fetch users: ${usersRes.status}`);
      const usersData: UsersApiResponse = await usersRes.json();
      const user = usersData.data?.[0];
      if (!user) {
        setGraphData({ nodes: [], edges: [] });
        setLoading(false);
        return;
      }
      setUserName(user.name ?? user.id);
      setUserId(user.id);

      // Step 2: fetch nodes for the user
      const nodesRes = await fetch(
        `${API_BASE}/v1/users/${user.id}/graph/nodes?limit=100`,
        { headers: authHeaders() },
      );
      if (!nodesRes.ok) throw new Error(`Failed to fetch nodes: ${nodesRes.status}`);
      const nodesData: NodesApiResponse = await nodesRes.json();
      const nodes: GraphNodeData[] = nodesData.data?.items ?? [];

      if (nodes.length === 0) {
        setGraphData({ nodes: [], edges: [] });
        setLoading(false);
        return;
      }

      // Step 3: fetch edges for each node in batches of 5
      const allEdges: GraphEdgeData[] = [];
      const seen = new Set<string>();

      for (let i = 0; i < nodes.length; i += 5) {
        const batch = nodes.slice(i, i + 5);
        const batchResults = await Promise.allSettled(
          batch.map((node) =>
            fetch(
              `${API_BASE}/v1/users/${user.id}/graph/edges?subject_id=${node.id}&limit=50`,
              { headers: authHeaders() },
            )
              .then((r) => {
                if (!r.ok) throw new Error(`Edge fetch failed for ${node.id}: ${r.status}`);
                return r.json() as Promise<EdgesApiResponse>;
              })
              .then((d) => d.data?.items ?? []),
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

      setGraphData({ nodes, edges: allEdges });
    } catch {
      setHasError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  return (
    <RequireAuth>
      <div className="space-y-6">
        {/* ── Page header ──────────────────────────────────────────────── */}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">Graph Explorer</h1>
            <p className="text-sm text-surface-400 mt-1">
              Explore entities and relationships in your knowledge graph
              {userName && (
                <>
                  {" · "}
                  <span className="text-accent-300">{userName}</span>
                </>
              )}
            </p>
          </div>
          <button
            onClick={loadData}
            disabled={loading}
            className="btn-secondary text-sm gap-2 flex items-center"
          >
            <RotateCcw size={14} className={loading ? "animate-spin" : ""} />
            Refresh
          </button>
        </div>

        {/* ── Force Graph ─────────────────────────────────────────────── */}
        <ForceGraph
          nodes={graphData?.nodes ?? []}
          edges={graphData?.edges ?? []}
          loading={loading}
          error={hasError ? "Failed to load graph" : null}
          onRetry={loadData}
          userName={userName}
          apiConfig={{
            baseUrl: API_BASE,
            userId,
            headers: authHeaders(),
          }}
          showFilter
          showControls
          showLegend
        />
      </div>
    </RequireAuth>
  );
}
