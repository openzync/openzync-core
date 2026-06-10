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
import { listGraphNodes, ApiError } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";

type GraphNode = components["schemas"]["GraphNode"];

interface GraphNodeRow {
  id: string;
  name: string;
  type: string;
  summary: string | null;
  created_at: string;
}

// ─── Component Props ──────────────────────────────────────────────────────────

interface GraphTabProps {
  userId: string;
  sessionId: string;
}

// ─── Component ────────────────────────────────────────────────────────────────

export default function GraphTab({ userId }: GraphTabProps) {
  const [nodes, setNodes] = useState<GraphNodeRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [entityFilter, setEntityFilter] = useState("");
  const canvasRef = useRef<HTMLDivElement>(null);
  const graphInstance = useRef<{ destroy: () => void } | null>(null);

  // ── Fetch nodes ─────────────────────────────────────────────────────────────

  const buildGraphData = useCallback(
    (nodeList: GraphNodeRow[]) => {
      if (!canvasRef.current) return;

      // Clean up previous graph
      if (graphInstance.current) {
        graphInstance.current.destroy();
        graphInstance.current = null;
      }
      // Clear canvas
      canvasRef.current.innerHTML = "";

      if (nodeList.length === 0) return;

      // Build nodes and edges for the force graph
      const graphNodes = nodeList.map((n, i) => ({
        id: n.id,
        name: n.name,
        type: n.type,
        val: 5,
      }));

      // Since we don't have edges from the list endpoint directly,
      // create a simple force layout with nodes grouped by type
      // Edges will be shown connecting nodes of the same type loosely
      const graphEdges: { source: string; target: string }[] = [];
      const typeGroups: Record<string, string[]> = {};
      for (const n of nodeList) {
        if (!typeGroups[n.type]) typeGroups[n.type] = [];
        typeGroups[n.type].push(n.id);
      }

      // Add edges within type groups to show clustering
      for (const ids of Object.values(typeGroups)) {
        for (let i = 0; i < ids.length - 1; i++) {
          graphEdges.push({ source: ids[i], target: ids[i + 1] });
        }
      }

      // Dynamically import ForceGraph2D (SSR-safe)
      import("react-force-graph-2d").then((mod) => {
        const ForceGraph2D = mod.default;
        // We need to use React to render, so let's use a different approach
        // Create a canvas-based rendering directly
        const container = canvasRef.current;
        if (!container) return;

        const width = container.clientWidth || 800;
        const height = 500;

        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        container.appendChild(canvas);

        // Simple force-directed layout simulation
        const simNodes = graphNodes.map((n) => ({
          ...n,
          x: Math.random() * width,
          y: Math.random() * height,
          vx: 0,
          vy: 0,
        }));

        const simEdges = graphEdges.map((e) => ({
          source: simNodes.findIndex((n) => n.id === e.source),
          target: simNodes.findIndex((n) => n.id === e.target),
        }));

        let animFrame: number;
        const ctx = canvas.getContext("2d")!;

        const typeColors: Record<string, string> = {
          person: "#1565C0",
          organization: "#388E3C",
          location: "#F57C00",
          event: "#7B1FA2",
          concept: "#C62828",
          literal: "#6A1B9A",
        };

        function getColor(type: string): string {
          return typeColors[type.toLowerCase()] || "#757575";
        }

        function simulationStep() {
          // Simple force simulation
          const repulsion = 5000;
          const attraction = 0.005;
          const damping = 0.9;
          const centerForce = 0.01;

          // Center
          const cx = width / 2;
          const cy = height / 2;

          for (let i = 0; i < simNodes.length; i++) {
            const n = simNodes[i];

            // Center force
            n.vx += (cx - n.x) * centerForce;
            n.vy += (cy - n.y) * centerForce;

            // Repulsion between all nodes
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

            // Attraction along edges
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

          // Apply velocity
          let totalVelocity = 0;
          for (const n of simNodes) {
            n.vx *= damping;
            n.vy *= damping;
            n.x += n.vx;
            n.y += n.vy;
            totalVelocity += Math.abs(n.vx) + Math.abs(n.vy);
          }

          // Draw
          ctx.clearRect(0, 0, width, height);

          // Edges
          ctx.strokeStyle = "rgba(0,0,0,0.08)";
          ctx.lineWidth = 1;
          for (const edge of simEdges) {
            const s = simNodes[edge.source];
            const t = simNodes[edge.target];
            ctx.beginPath();
            ctx.moveTo(s.x, s.y);
            ctx.lineTo(t.x, t.y);
            ctx.stroke();
          }

          // Nodes
          for (const n of simNodes) {
            const color = getColor(n.type);
            const radius = 8;

            // Glow
            ctx.beginPath();
            ctx.arc(n.x, n.y, radius + 4, 0, 2 * Math.PI);
            ctx.fillStyle = color + "20";
            ctx.fill();

            // Main circle
            ctx.beginPath();
            ctx.arc(n.x, n.y, radius, 0, 2 * Math.PI);
            ctx.fillStyle = color;
            ctx.fill();
            ctx.strokeStyle = "#fff";
            ctx.lineWidth = 2;
            ctx.stroke();

            // Label
            ctx.fillStyle = "#333";
            ctx.font = "11px Inter, system-ui, sans-serif";
            ctx.textAlign = "center";
            ctx.fillText(n.name, n.x, n.y + radius + 16);

            // Type badge
            ctx.fillStyle = "rgba(0,0,0,0.4)";
            ctx.font = "9px Inter, system-ui, sans-serif";
            ctx.fillText(n.type, n.x, n.y + radius + 28);
          }

          if (totalVelocity > 0.1) {
            animFrame = requestAnimationFrame(simulationStep);
          }
        }

        animFrame = requestAnimationFrame(simulationStep);

        graphInstance.current = {
          destroy: () => {
            cancelAnimationFrame(animFrame);
          },
        };
      });
    },
    [],
  );

  const fetchNodes = useCallback(async () => {
    setLoading(true);
    try {
      const params: Record<string, string | number | boolean | undefined | null> = {
        limit: 100,
      };
      if (entityFilter) {
        params.entity_type = entityFilter;
      }
      const result = await listGraphNodes(userId, params);
      const nodeList = (result.data?.items ?? []) as GraphNodeRow[];
      setNodes(nodeList);
      setCursor(result.data?.next_cursor ?? null);
      setHasMore(result.data?.has_more ?? false);
      // Build graph after state is updated
      setTimeout(() => buildGraphData(nodeList), 0);
    } catch (err) {
      console.error("Failed to load graph nodes", err);
    } finally {
      setLoading(false);
    }
  }, [userId, entityFilter, buildGraphData]);

  const loadMore = useCallback(async () => {
    if (loadingMore || !cursor) return;
    setLoadingMore(true);
    try {
      const result = await listGraphNodes(userId, {
        limit: 100,
        cursor,
        entity_type: entityFilter || null,
      });
      const newNodes = (result.data?.items ?? []) as GraphNodeRow[];
      const combined = [...nodes, ...newNodes];
      setNodes(combined);
      setCursor(result.data?.next_cursor ?? null);
      setHasMore(result.data?.has_more ?? false);
      setTimeout(() => buildGraphData(combined), 0);
    } catch (err) {
      console.error("Failed to load more nodes", err);
    } finally {
      setLoadingMore(false);
    }
  }, [loadingMore, cursor, userId, entityFilter, nodes, buildGraphData]);

  useEffect(() => {
    fetchNodes();
    return () => {
      if (graphInstance.current) {
        graphInstance.current.destroy();
      }
    };
  }, [fetchNodes]);

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
        <Button variant="outlined" size="small" onClick={fetchNodes}>
          Refresh
        </Button>
        <Typography variant="caption" color="text.secondary">
          {nodes.length} entities
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
              {[
                { type: "person", label: "Person", color: "#1565C0" },
                { type: "organization", label: "Organization", color: "#388E3C" },
                { type: "location", label: "Location", color: "#F57C00" },
                { type: "event", label: "Event", color: "#7B1FA2" },
                { type: "concept", label: "Concept", color: "#C62828" },
              ].map((item) => (
                <Box key={item.type} sx={{ display: "flex", alignItems: "center", gap: 0.5 }}>
                  <Box
                    sx={{
                      width: 12,
                      height: 12,
                      borderRadius: "50%",
                      bgcolor: item.color,
                    }}
                  />
                  <Typography variant="caption">{item.label}</Typography>
                </Box>
              ))}
            </Box>
          </Card>

          {hasMore && (
            <Box sx={{ display: "flex", justifyContent: "center", mt: 2 }}>
              <Button
                variant="outlined"
                onClick={loadMore}
                disabled={loadingMore}
                startIcon={loadingMore ? <CircularProgress size={16} /> : undefined}
              >
                {loadingMore ? "Loading..." : "Load More Entities"}
              </Button>
            </Box>
          )}
        </>
      )}
    </Box>
  );
}
