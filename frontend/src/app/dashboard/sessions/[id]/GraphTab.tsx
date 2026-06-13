"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import {
  Box,
  Card,
  Typography,
  CircularProgress,
  Chip,
  TextField,
  Button,
} from "@mui/material";
import { listGraphNodes, listGraphEdges, ApiError } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";

type GraphNode = components["schemas"]["GraphNode"];

interface GraphNodeRow {
  id: string;
  name: string;
  type: string;
  summary: string | null;
  created_at: string;
}

interface EdgeData {
  id: string;
  source_id: string;
  target_id: string;
  type: string;
}

// ─── Component Props ──────────────────────────────────────────────────────────

interface GraphTabProps {
  projectId: string;
  userId: string;
  sessionId: string;
}

// ─── Component ────────────────────────────────────────────────────────────────

export default function GraphTab({ projectId, userId }: GraphTabProps) {
  const [nodes, setNodes] = useState<GraphNodeRow[]>([]);
  const [edges, setEdges] = useState<EdgeData[]>([]);
  const [loading, setLoading] = useState(true);
  const [edgeLoading, setEdgeLoading] = useState(false);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [entityFilter, setEntityFilter] = useState("");
  const canvasRef = useRef<HTMLDivElement>(null);
  const graphInstance = useRef<{ destroy: () => void } | null>(null);

  // ── Canvas rendering ────────────────────────────────────────────────────────

  const TYPE_COLORS: Record<string, string> = {
    person: "#1565C0",
    organization: "#388E3C",
    location: "#F57C00",
    event: "#7B1FA2",
    concept: "#C62828",
    literal: "#6A1B9A",
  };

  function getColor(type: string): string {
    return TYPE_COLORS[type.toLowerCase()] || "#757575";
  }

  const buildGraph = useCallback(
    (nodeList: GraphNodeRow[], edgeList: EdgeData[]) => {
      if (!canvasRef.current) return;

      if (graphInstance.current) {
        graphInstance.current.destroy();
        graphInstance.current = null;
      }
      canvasRef.current.innerHTML = "";
      if (nodeList.length === 0) return;

      const container = canvasRef.current;
      const width = container.clientWidth || 800;
      const height = 500;

      const canvas = document.createElement("canvas");
      canvas.width = width;
      canvas.height = height;
      container.appendChild(canvas);

      const ctx = canvas.getContext("2d")!;

      const simNodes = nodeList.map((n) => ({
        id: n.id,
        name: n.name,
        type: n.type,
        x: Math.random() * width,
        y: Math.random() * height,
        vx: 0,
        vy: 0,
      }));

      // Build real edges from API, deduplicated
      const nodeIndex = new Map(simNodes.map((n, i) => [n.id, i]));
      const seenEdges = new Set<string>();
      const simEdges: { source: number; target: number; type: string }[] = [];
      for (const e of edgeList) {
        const key = [e.source_id, e.target_id].sort().join("|");
        if (seenEdges.has(key)) continue;
        seenEdges.add(key);
        const si = nodeIndex.get(e.source_id);
        const ti = nodeIndex.get(e.target_id);
        if (si !== undefined && ti !== undefined) {
          simEdges.push({ source: si, target: ti, type: e.type });
        }
      }

      // ── Hover / click state ──────────────────────────────────────────────
      let hoveredNode: number | null = null;
      let selectedNodeId: string | null = null;

      let animFrame: number;

      function step() {
        const repulsion = 8000;
        const attraction = 0.003;
        const damping = 0.9;
        const centerForce = 0.008;
        const cx = width / 2;
        const cy = height / 2;

        for (let i = 0; i < simNodes.length; i++) {
          const n = simNodes[i];

          n.vx += (cx - n.x) * centerForce;
          n.vy += (cy - n.y) * centerForce;

          for (let j = i + 1; j < simNodes.length; j++) {
            const other = simNodes[j];
            const dx = n.x - other.x;
            const dy = n.y - other.y;
            const dist = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
            const force = repulsion / (dist * dist);
            const fx = (dx / dist) * force;
            const fy = (dy / dist) * force;
            n.vx += fx;
            n.vy += fy;
            other.vx -= fx;
            other.vy -= fy;
          }

          for (const edge of simEdges) {
            if (edge.source === i) {
              const target = simNodes[edge.target];
              const dx = target.x - n.x;
              const dy = target.y - n.y;
              n.vx += dx * attraction;
              n.vy += dy * attraction;
              target.vx -= dx * attraction;
              target.vy -= dy * attraction;
            }
          }
        }

        let totalVel = 0;
        for (const n of simNodes) {
          n.vx *= damping;
          n.vy *= damping;
          n.x += n.vx;
          n.y += n.vy;
          totalVel += Math.abs(n.vx) + Math.abs(n.vy);
        }

        ctx.clearRect(0, 0, width, height);

        // ── Draw edges ────────────────────────────────────────────────────
        for (const edge of simEdges) {
          const s = simNodes[edge.source];
          const t = simNodes[edge.target];

          const isHighlighted =
            hoveredNode !== null &&
            (edge.source === hoveredNode || edge.target === hoveredNode);

          ctx.strokeStyle = isHighlighted
            ? "rgba(0,0,0,0.45)"
            : "rgba(0,0,0,0.2)";
          ctx.lineWidth = isHighlighted ? 2 : 1;
          ctx.beginPath();
          ctx.moveTo(s.x, s.y);
          ctx.lineTo(t.x, t.y);
          ctx.stroke();

          // Edge label
          const midX = (s.x + t.x) / 2;
          const midY = (s.y + t.y) / 2;
          ctx.fillStyle = "rgba(0,0,0,0.35)";
          ctx.font = "9px Inter, system-ui, sans-serif";
          ctx.textAlign = "center";
          ctx.fillText(edge.type, midX, midY - 4);
        }

        // ── Draw nodes ────────────────────────────────────────────────────
        for (const n of simNodes) {
          const color = getColor(n.type);
          const isSelected = n.id === selectedNodeId;
          const isHovered = hoveredNode === simNodes.indexOf(n);
          const radius = isSelected ? 12 : isHovered ? 10 : 8;
          const glow = isSelected ? 6 : 4;

          ctx.beginPath();
          ctx.arc(n.x, n.y, radius + glow, 0, 2 * Math.PI);
          ctx.fillStyle = color + (isSelected || isHovered ? "30" : "15");
          ctx.fill();

          ctx.beginPath();
          ctx.arc(n.x, n.y, radius, 0, 2 * Math.PI);
          ctx.fillStyle = color;
          ctx.fill();
          ctx.strokeStyle = isSelected || isHovered ? "#fff" : "transparent";
          ctx.lineWidth = 3;
          ctx.stroke();

          ctx.fillStyle = "#333";
          ctx.font = "11px Inter, system-ui, sans-serif";
          ctx.textAlign = "center";
          ctx.fillText(n.name, n.x, n.y + radius + 16);
        }

        if (totalVel > 0.1) {
          animFrame = requestAnimationFrame(step);
        }
      }

      // Hover detection
      canvas.onmousemove = (e) => {
        const rect = canvas.getBoundingClientRect();
        const mx = e.clientX - rect.left;
        const my = e.clientY - rect.top;
        const found = simNodes.findIndex((n) => {
          const dx = mx - n.x;
          const dy = my - n.y;
          return Math.sqrt(dx * dx + dy * dy) < 15;
        });
        hoveredNode = found >= 0 ? found : null;
        canvas.style.cursor = hoveredNode !== null ? "pointer" : "default";
      };

      canvas.onclick = (e) => {
        const rect = canvas.getBoundingClientRect();
        const mx = e.clientX - rect.left;
        const my = e.clientY - rect.top;
        const found = simNodes.find((n) => {
          const dx = mx - n.x;
          const dy = my - n.y;
          return Math.sqrt(dx * dx + dy * dy) < 15;
        });
        selectedNodeId = found?.id ?? null;
      };

      animFrame = requestAnimationFrame(step);

      graphInstance.current = {
        destroy: () => cancelAnimationFrame(animFrame),
      };
    },
    [],
  );

  // ── Fetch nodes + edges ─────────────────────────────────────────────────────

  const fetchGraph = useCallback(async () => {
    setLoading(true);
    try {
      const params: Record<string, string | number | boolean | undefined | null> = {
        limit: 100,
      };
      if (entityFilter) {
        params.entity_type = entityFilter;
      }
      const result = await listGraphNodes(projectId, userId, params);
      const nodeList = (result.data?.items ?? []) as GraphNodeRow[];
      setNodes(nodeList);
      setCursor(result.data?.next_cursor ?? null);
      setHasMore(result.data?.has_more ?? false);

      // Fetch real edges for all nodes
      setEdgeLoading(true);
      const allEdges: EdgeData[] = [];
      const seen = new Set<string>();
      const batchSize = 5;
      for (let i = 0; i < nodeList.length; i += batchSize) {
        const batch = nodeList.slice(i, i + batchSize);
        const results = await Promise.allSettled(
          batch.map((n) =>
            listGraphEdges(projectId, userId, { subject_id: n.id, limit: 50 }).catch(
              () => ({ data: { items: [] } }),
            ),
          ),
        );
        for (const r of results) {
          if (r.status === "fulfilled") {
            const items = ((r.value.data as { items?: EdgeData[] })?.items ?? []) as EdgeData[];
            for (const e of items) {
              const key = [e.source_id, e.target_id].sort().join("|");
              if (!seen.has(key)) {
                seen.add(key);
                allEdges.push(e);
              }
            }
          }
        }
      }
      setEdges(allEdges);
      setEdgeLoading(false);
    } catch (err) {
      console.error("Failed to load graph", err);
    } finally {
      setLoading(false);
    }
  }, [projectId, userId, entityFilter, buildGraph]);

  const loadMore = useCallback(async () => {
    if (loading || !cursor) return;
    setLoading(true);
    try {
      const result = await listGraphNodes(projectId, userId, {
        limit: 100,
        cursor,
        entity_type: entityFilter || null,
      });
      const newNodes = (result.data?.items ?? []) as GraphNodeRow[];
      const combined = [...nodes, ...newNodes];
      setNodes(combined);
      setCursor(result.data?.next_cursor ?? null);
      setHasMore(result.data?.has_more ?? false);
      // Fetch edges for new nodes too
      const allEdges = [...edges];
      const seen = new Set(allEdges.map((e) => [e.source_id, e.target_id].sort().join("|")));
      for (const n of newNodes) {
        try {
          const r = await listGraphEdges(projectId, userId, { subject_id: n.id, limit: 50 });
          const items = ((r.data as { items?: EdgeData[] })?.items ?? []) as EdgeData[];
          for (const e of items) {
            const key = [e.source_id, e.target_id].sort().join("|");
            if (!seen.has(key)) {
              seen.add(key);
              allEdges.push(e);
            }
          }
        } catch {
          // ignore
        }
      }
      setEdges(allEdges);
    } catch (err) {
      console.error("Failed to load more nodes", err);
    } finally {
      setLoading(false);
    }
  }, [loading, cursor, projectId, userId, entityFilter, nodes, edges, buildGraph]);

  // Rebuild canvas whenever nodes/edges/loading settle
  useEffect(() => {
    if (!loading && nodes.length > 0) {
      buildGraph(nodes, edges);
    }
    return () => {
      if (graphInstance.current) {
        graphInstance.current.destroy();
      }
    };
  }, [nodes, edges, loading, buildGraph]);

  useEffect(() => {
    fetchGraph();
    return () => {
      if (graphInstance.current) {
        graphInstance.current.destroy();
      }
    };
  }, [fetchGraph]);

  // ── Render ──────────────────────────────────────────────────────────────────

  if (loading) {
    return (
      <Box sx={{ display: "flex", justifyContent: "center", py: 6 }}>
        <CircularProgress />
      </Box>
    );
  }

  return (
    <Box>
      {/* Controls */}
      <Box sx={{ display: "flex", gap: 2, mb: 2, alignItems: "center" }}>
        <TextField
          size="small"
          label="Filter by entity type"
          value={entityFilter}
          onChange={(e) => setEntityFilter(e.target.value)}
          sx={{ minWidth: 200 }}
        />
        <Button variant="outlined" size="small" onClick={fetchGraph}>
          Refresh
        </Button>
        <Typography variant="caption" color="text.secondary">
          {nodes.length} entities · {edges.length} relationships
          {edgeLoading && " (loading edges...)"}
        </Typography>
      </Box>

      {nodes.length === 0 ? (
        <Card sx={{ p: 4, textAlign: "center" }}>
          <Typography variant="body1" color="text.secondary">
            No graph entities found. Entities appear after messages are processed
            by the entity extraction worker.
          </Typography>
        </Card>
      ) : (
        <>
          {/* Force graph canvas */}
          <Card
            ref={canvasRef}
            sx={{
              width: "100%",
              height: 500,
              overflow: "hidden",
              position: "relative",
              mb: 2,
            }}
          />

          {/* Legend */}
          <Card sx={{ p: 2 }}>
            <Typography variant="subtitle2" gutterBottom>
              Legend
            </Typography>
            <Box sx={{ display: "flex", gap: 2, flexWrap: "wrap" }}>
              {Object.entries(TYPE_COLORS).map(([type, color]) => (
                <Box key={type} sx={{ display: "flex", alignItems: "center", gap: 0.5 }}>
                  <Box
                    sx={{
                      width: 12,
                      height: 12,
                      borderRadius: "50%",
                      bgcolor: color,
                    }}
                  />
                  <Typography variant="caption" sx={{ textTransform: "capitalize" }}>
                    {type}
                  </Typography>
                </Box>
              ))}
            </Box>
          </Card>

          {hasMore && (
            <Box sx={{ display: "flex", justifyContent: "center", mt: 2 }}>
              <Button
                variant="outlined"
                onClick={loadMore}
                disabled={loading}
                startIcon={loading ? <CircularProgress size={16} /> : undefined}
              >
                {loading ? "Loading..." : "Load More Entities"}
              </Button>
            </Box>
          )}
        </>
      )}
    </Box>
  );
}
