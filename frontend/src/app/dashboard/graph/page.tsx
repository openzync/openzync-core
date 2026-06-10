"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import {
  Box,
  Typography,
  Card,
  TextField,
  Button,
  CircularProgress,
  Chip,
} from "@mui/material";
import { listGraphNodes, listGraphEdges, ApiError } from "@/lib/api/client";
import { useAuth } from "@/lib/auth/useAuth";
import { listUsers } from "@/lib/api/client";

// ─── Types ────────────────────────────────────────────────────────────────────

interface NodeData {
  id: string;
  name: string;
  type: string;
  summary: string | null;
  x?: number;
  y?: number;
  vx?: number;
  vy?: number;
}

interface EdgeData {
  id: string;
  source_id: string;
  target_id: string;
  type: string;
}

// ─── Colors ───────────────────────────────────────────────────────────────────

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

// ─── Page Component ───────────────────────────────────────────────────────────

export default function GraphPage() {
  useAuth();
  const canvasRef = useRef<HTMLDivElement>(null);
  const graphInstance = useRef<{ destroy: () => void } | null>(null);

  const [nodes, setNodes] = useState<NodeData[]>([]);
  const [edges, setEdges] = useState<EdgeData[]>([]);
  const [loading, setLoading] = useState(true);
  const [entityFilter, setEntityFilter] = useState("");
  const [selectedNode, setSelectedNode] = useState<NodeData | null>(null);
  const [userId, setUserId] = useState<string>("");

  // ── Fetch users for selector ─────────────────────────────────────────────────

  useEffect(() => {
    listUsers({ limit: 1 })
      .then((result) => {
        const users = result.data as { id: string; external_id: string }[];
        if (users.length > 0) setUserId(users[0].id);
      })
      .catch(() => {});
  }, []);

  // ── Build graph ──────────────────────────────────────────────────────────────

  const buildGraph = useCallback(
    (nodeList: NodeData[], edgeList: EdgeData[]) => {
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
        ...n,
        x: Math.random() * width,
        y: Math.random() * height,
        vx: 0,
        vy: 0,
      }));

      // Build edge index from API data, deduplicated
      const nodeIndex = new Map(simNodes.map((n, i) => [n.id, i]));
      const seenPairs = new Set<string>();
      const simEdges: { source: number; target: number; type: string }[] = [];
      for (const e of edgeList) {
        const key = [e.source_id, e.target_id].sort().join("|");
        if (seenPairs.has(key)) continue;
        seenPairs.add(key);
        const si = nodeIndex.get(e.source_id);
        const ti = nodeIndex.get(e.target_id);
        if (si !== undefined && ti !== undefined) {
          simEdges.push({ source: si, target: ti, type: e.type });
        }
      }

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

          // Center force
          n.vx! += (cx - n.x!) * centerForce;
          n.vy! += (cy - n.y!) * centerForce;

          // Repulsion
          for (let j = i + 1; j < simNodes.length; j++) {
            const other = simNodes[j];
            const dx = n.x! - other.x!;
            const dy = n.y! - other.y!;
            const dist = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
            const force = repulsion / (dist * dist);
            const fx = (dx / dist) * force;
            const fy = (dy / dist) * force;
            n.vx! += fx;
            n.vy! += fy;
            other.vx! -= fx;
            other.vy! -= fy;
          }

          // Attraction along edges
          for (const edge of simEdges) {
            if (edge.source === i) {
              const target = simNodes[edge.target];
              const dx = target.x! - n.x!;
              const dy = target.y! - n.y!;
              n.vx! += dx * attraction;
              n.vy! += dy * attraction;
              target.vx! -= dx * attraction;
              target.vy! -= dy * attraction;
            }
          }
        }

        let totalVel = 0;
        for (const n of simNodes) {
          n.vx! *= damping;
          n.vy! *= damping;
          n.x! += n.vx!;
          n.y! += n.vy!;
          totalVel += Math.abs(n.vx!) + Math.abs(n.vy!);
        }

        ctx.clearRect(0, 0, width, height);

        // Edges
        for (const edge of simEdges) {
          const s = simNodes[edge.source];
          const t = simNodes[edge.target];

          const isHighlighted =
            selectedNode !== null &&
            (simNodes[edge.source].id === selectedNode.id ||
             simNodes[edge.target].id === selectedNode.id);

          ctx.strokeStyle = isHighlighted
            ? "rgba(0,0,0,0.5)"
            : "rgba(0,0,0,0.25)";
          ctx.lineWidth = isHighlighted ? 2 : 1;
          ctx.beginPath();
          ctx.moveTo(s.x!, s.y!);
          ctx.lineTo(t.x!, t.y!);
          ctx.stroke();

          // Edge type label
          const midX = (s.x! + t.x!) / 2;
          const midY = (s.y! + t.y!) / 2;
          ctx.fillStyle = "rgba(0,0,0,0.35)";
          ctx.font = "9px Inter, system-ui, sans-serif";
          ctx.textAlign = "center";
          ctx.fillText(edge.type, midX, midY - 4);
        }

        // Nodes
        for (const n of simNodes) {
          const color = getColor(n.type);
          const radius = n.id === selectedNode?.id ? 12 : 8;
          const glow = n.id === selectedNode?.id ? 6 : 4;

          // Glow
          ctx.beginPath();
          ctx.arc(n.x!, n.y!, radius + glow, 0, 2 * Math.PI);
          ctx.fillStyle = color + (n.id === selectedNode?.id ? "30" : "15");
          ctx.fill();

          // Circle
          ctx.beginPath();
          ctx.arc(n.x!, n.y!, radius, 0, 2 * Math.PI);
          ctx.fillStyle = color;
          ctx.fill();
          ctx.strokeStyle = n.id === selectedNode?.id ? "#fff" : "transparent";
          ctx.lineWidth = 3;
          ctx.stroke();

          // Label
          ctx.fillStyle = "#333";
          ctx.font = "11px Inter, system-ui, sans-serif";
          ctx.textAlign = "center";
          ctx.fillText(n.name, n.x!, n.y! + radius + 16);
        }

        // Hover detection
        canvas.onmousemove = (e) => {
          const rect = canvas.getBoundingClientRect();
          const mx = e.clientX - rect.left;
          const my = e.clientY - rect.top;
          const found = simNodes.find((n) => {
            const dx = mx - n.x!;
            const dy = my - n.y!;
            return Math.sqrt(dx * dx + dy * dy) < 15;
          });
          canvas.style.cursor = found ? "pointer" : "default";
        };

        canvas.onclick = (e) => {
          const rect = canvas.getBoundingClientRect();
          const mx = e.clientX - rect.left;
          const my = e.clientY - rect.top;
          const found = simNodes.find((n) => {
            const dx = mx - n.x!;
            const dy = my - n.y!;
            return Math.sqrt(dx * dx + dy * dy) < 15;
          });
          setSelectedNode(found ?? null);
        };

        if (totalVel > 0.1) {
          animFrame = requestAnimationFrame(step);
        }
      }

      animFrame = requestAnimationFrame(step);

      graphInstance.current = {
        destroy: () => cancelAnimationFrame(animFrame),
      };
    },
    [selectedNode],
  );

  // ── Fetch data ──────────────────────────────────────────────────────────────

  const fetchGraph = useCallback(async () => {
    if (!userId) return;
    setLoading(true);
    try {
      const params: Record<string, string | number | boolean | undefined | null> = {
        limit: 100,
      };
      if (entityFilter) params.entity_type = entityFilter;

      const nodeResult = await listGraphNodes(userId, params);
      const nodeList = (nodeResult.data?.items as NodeData[]) ?? [];

      // Fetch real edges for all nodes, deduplicated
      let edgeList: EdgeData[] = [];
      const seenEdges = new Set<string>();
      if (nodeList.length > 0) {
        const batchSize = 5;
        for (let i = 0; i < nodeList.length; i += batchSize) {
          const batch = nodeList.slice(i, i + batchSize);
          const results = await Promise.allSettled(
            batch.map((n) =>
              listGraphEdges(userId, { subject_id: n.id, limit: 50 }).catch(
                () => ({ data: { items: [] } }),
              ),
            ),
          );
          for (const r of results) {
            if (r.status === "fulfilled") {
              const items = ((r.value.data as { items?: EdgeData[] })?.items ?? []) as EdgeData[];
              for (const e of items) {
                const key = [e.source_id, e.target_id].sort().join("|");
                if (!seenEdges.has(key)) {
                  seenEdges.add(key);
                  edgeList.push(e);
                }
              }
            }
          }
        }
      }

      setNodes(nodeList);
      setEdges(edgeList);
    } catch (err) {
      console.error("Failed to load graph", err);
    } finally {
      setLoading(false);
    }
  }, [userId, entityFilter, buildGraph]);

  useEffect(() => {
    fetchGraph();
    return () => {
      if (graphInstance.current) graphInstance.current.destroy();
    };
  }, [fetchGraph]);

  // Rebuild canvas whenever nodes/edges change
  useEffect(() => {
    if (!loading && nodes.length > 0) {
      buildGraph(nodes, edges);
    }
    return () => {
      if (graphInstance.current) graphInstance.current.destroy();
    };
  }, [nodes, edges, loading, buildGraph]);

  // ── Render ──────────────────────────────────────────────────────────────────

  return (
    <Box>
      <Typography variant="h4" gutterBottom sx={{ fontWeight: 600 }}>
        Knowledge Graph
      </Typography>
      <Typography variant="subtitle1" sx={{ mb: 3 }}>
        Explore entities and relationships extracted from your data
      </Typography>

      {/* Controls */}
      <Box sx={{ display: "flex", gap: 2, mb: 3, alignItems: "center", flexWrap: "wrap" }}>
        <TextField
          size="small"
          label="Entity type filter"
          value={entityFilter}
          onChange={(e) => setEntityFilter(e.target.value)}
          sx={{ minWidth: 180 }}
        />
        <Button variant="contained" size="small" onClick={fetchGraph} disabled={loading}>
          {loading ? "Loading..." : "Refresh"}
        </Button>
        <Typography variant="body2" color="text.secondary">
          {nodes.length} entities · {edges.length} relationships
        </Typography>
      </Box>

      {loading ? (
        <Box sx={{ display: "flex", justifyContent: "center", py: 8 }}>
          <CircularProgress />
        </Box>
      ) : nodes.length === 0 ? (
        <Card sx={{ p: 4, textAlign: "center" }}>
          <Typography variant="body1" color="text.secondary">
            No graph entities found. Entities are created when messages are
            processed by the extraction worker.
          </Typography>
        </Card>
      ) : (
        <>
          {/* Graph canvas */}
          <Card
            ref={canvasRef}
            sx={{ width: "100%", height: 500, overflow: "hidden", mb: 2 }}
          />

          {/* Selected node info */}
          {selectedNode && (
            <Card sx={{ p: 2, mb: 2 }}>
              <Typography variant="subtitle2" gutterBottom>
                Selected: {selectedNode.name}
              </Typography>
              <Box sx={{ display: "flex", gap: 2, flexWrap: "wrap" }}>
                <Chip label={selectedNode.type} size="small" />
                <Typography variant="body2" color="text.secondary">
                  ID: {selectedNode.id}
                </Typography>
                {selectedNode.summary && (
                  <Typography variant="body2">{selectedNode.summary}</Typography>
                )}
              </Box>
            </Card>
          )}

          {/* Legend */}
          <Card sx={{ p: 2 }}>
            <Typography variant="subtitle2" gutterBottom>
              Legend
            </Typography>
            <Box sx={{ display: "flex", gap: 2, flexWrap: "wrap" }}>
              {Object.entries(TYPE_COLORS).map(([type, color]) => (
                <Box key={type} sx={{ display: "flex", alignItems: "center", gap: 0.5 }}>
                  <Box sx={{ width: 12, height: 12, borderRadius: "50%", bgcolor: color }} />
                  <Typography variant="caption" sx={{ textTransform: "capitalize" }}>
                    {type}
                  </Typography>
                </Box>
              ))}
            </Box>
          </Card>
        </>
      )}
    </Box>
  );
}
