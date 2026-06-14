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
  useTheme,
} from "@mui/material";
import ForceGraph2D from "react-force-graph-2d";
import { listGraphNodes, listGraphEdges } from "@/lib/api/client";
// ─── Types ─────────────────────────────────────────────────────────────────────

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

interface GraphNode {
  id: string;
  name: string;
  type: string;
  summary: string | null;
  val?: number;
  x?: number;
  y?: number;
  vx?: number;
  vy?: number;
}

interface GraphLink {
  source: string;
  target: string;
  type: string;
}

// ─── Colors ────────────────────────────────────────────────────────────────────

const TYPE_COLORS: Record<string, string> = {
  person: "#14488C",
  organization: "#1453A6",
  location: "#8FAFD9",
  event: "#1747A6",
  concept: "#6A8DB8",
  literal: "#4A6D96",
};

function getColor(type: string): string {
  return TYPE_COLORS[type.toLowerCase()] || "#757575";
}

// ─── Component Props ──────────────────────────────────────────────────────────

interface GraphTabProps {
  userId: string;
  sessionId: string;
}

// ─── Component ────────────────────────────────────────────────────────────────

export default function GraphTab({ userId }: GraphTabProps) {
  const theme = useTheme();
  const fgRef = useRef<any>(null);

  const [nodes, setNodes] = useState<GraphNode[]>([]);
  const [links, setLinks] = useState<GraphLink[]>([]);
  const [loading, setLoading] = useState(true);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [entityFilter, setEntityFilter] = useState("");
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
  const [hoveredNode, setHoveredNode] = useState<GraphNode | null>(null);

  // ── Fetch graph data ────────────────────────────────────────────────────────

  const fetchGraph = useCallback(async () => {
    setLoading(true);
    setSelectedNode(null);
    try {
      const params: Record<string, string | number | boolean | undefined | null> = {
        limit: 100,
      };
      if (entityFilter) params.entity_type = entityFilter;

      const result = await listGraphNodes(userId, params);
      const nodeList = ((result.data as any)?.items ?? []) as GraphNodeRow[];
      setCursor((result.data as any)?.next_cursor ?? null);
      setHasMore((result.data as any)?.has_more ?? false);

      // Fetch edges for all nodes
      const allEdges: EdgeData[] = [];
      const seen = new Set<string>();
      const batchSize = 5;
      for (let i = 0; i < nodeList.length; i += batchSize) {
        const batch = nodeList.slice(i, i + batchSize);
        const results = await Promise.allSettled(
          batch.map((n) =>
            listGraphEdges(userId, { subject_id: n.id, limit: 50 }).catch(
              () => ({ data: { items: [] } } as any),
            ),
          ),
        );
        for (const r of results) {
          if (r.status === "fulfilled") {
            const items = ((r.value.data as any)?.items ?? []) as EdgeData[];
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

      // Map to ForceGraph format
      const fgNodes: GraphNode[] = nodeList.map((n) => ({
        id: n.id,
        name: n.name,
        type: n.type,
        summary: n.summary,
        val: 1,
      }));

      const nodeIds = new Set(fgNodes.map((n) => n.id));
      const fgLinks: GraphLink[] = allEdges
        .filter((e) => nodeIds.has(e.source_id) && nodeIds.has(e.target_id))
        .map((e) => ({
          source: e.source_id,
          target: e.target_id,
          type: e.type,
        }));

      setNodes(fgNodes);
      setLinks(fgLinks);
    } catch (err) {
      console.error("Failed to load graph", err);
    } finally {
      setLoading(false);
    }
  }, [userId, entityFilter]);

  const loadMore = useCallback(async () => {
    if (loading || !cursor) return;
    setLoading(true);
    try {
      const result = await listGraphNodes(userId, {
        limit: 100,
        cursor,
        entity_type: entityFilter || null,
      });
      const newNodes = ((result.data as any)?.items ?? []) as GraphNodeRow[];
      setCursor((result.data as any)?.next_cursor ?? null);
      setHasMore((result.data as any)?.has_more ?? false);

      // Add new nodes
      const existingIds = new Set(nodes.map((n) => n.id));
      const freshNodes = newNodes.filter((n) => !existingIds.has(n.id));
      const fgNewNodes: GraphNode[] = freshNodes.map((n) => ({
        id: n.id,
        name: n.name,
        type: n.type,
        summary: n.summary,
        val: 1,
      }));

      // Fetch edges for new nodes
      const allEdges = [...links];
      const seen = new Set(allEdges.map((e) => [e.source, e.target].sort().join("|")));
      for (const n of freshNodes) {
        try {
          const r = await listGraphEdges(userId, { subject_id: n.id, limit: 50 });
          const items = ((r.data as any)?.items ?? []) as EdgeData[];
          for (const e of items) {
            const key = [e.source_id, e.target_id].sort().join("|");
            if (!seen.has(key)) {
              seen.add(key);
              allEdges.push({
                source: e.source_id,
                target: e.target_id,
                type: e.type,
              } as GraphLink);
            }
          }
        } catch {
          // ignore
        }
      }

      setNodes((prev) => [...prev, ...fgNewNodes]);
      setLinks(allEdges);
    } catch (err) {
      console.error("Failed to load more nodes", err);
    } finally {
      setLoading(false);
    }
  }, [loading, cursor, userId, entityFilter, nodes, links]);

  useEffect(() => {
    fetchGraph();
  }, [fetchGraph]);

  // Zoom to fit on data change
  useEffect(() => {
    if (!loading && nodes.length > 0 && fgRef.current) {
      setTimeout(() => fgRef.current?.zoomToFit(400), 300);
    }
  }, [loading, nodes.length]);

  // ── Render ──────────────────────────────────────────────────────────────────

  const isDark = theme.palette.mode === "dark";

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
      <Box sx={{ display: "flex", gap: 2, mb: 2, alignItems: "center", flexWrap: "wrap" }}>
        <TextField
          size="small"
          label="Filter by entity type"
          value={entityFilter}
          onChange={(e) => setEntityFilter(e.target.value)}
          sx={{ minWidth: 200 }}
        />
        <Button variant="outlined" size="small" onClick={fetchGraph} disabled={loading}>
          Refresh
        </Button>
        <Typography variant="caption" color="text.secondary">
          {nodes.length} entities · {links.length} relationships
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
          {/* Force graph */}
          <Card sx={{ width: "100%", height: 500, overflow: "hidden", mb: 2, position: "relative" }}>
            <ForceGraph2D
              ref={fgRef}
              graphData={{ nodes, links }}
              nodeLabel="name"
              nodeColor={(node: any) => getColor(node.type)}
              nodeVal={(node: any) => (selectedNode?.id === node.id ? 2.5 : node.val || 1)}
              nodeCanvasObject={(node: any, ctx: CanvasRenderingContext2D, globalScale: number) => {
                const label = node.name;
                const fontSize = Math.max(8, 12 / globalScale);
                const color = getColor(node.type);
                const isSelected = selectedNode?.id === node.id;
                const isHovered = hoveredNode?.id === node.id;
                const radius = isSelected ? 7 : isHovered ? 6 : 5;

                // Glow
                ctx.beginPath();
                ctx.arc(node.x, node.y, radius + 4, 0, 2 * Math.PI);
                ctx.fillStyle = color + (isSelected || isHovered ? "30" : "15");
                ctx.fill();

                // Circle
                ctx.beginPath();
                ctx.arc(node.x, node.y, radius, 0, 2 * Math.PI);
                ctx.fillStyle = color;
                ctx.fill();
                ctx.strokeStyle = isSelected || isHovered
                  ? (isDark ? "#F2F2F2" : "#FFFFFF")
                  : "transparent";
                ctx.lineWidth = 2;
                ctx.stroke();

                // Label
                ctx.font = `${fontSize}px Inter, system-ui, sans-serif`;
                ctx.textAlign = "center";
                ctx.textBaseline = "top";
                ctx.fillStyle = isDark ? "rgba(242,242,242,0.8)" : "#333";
                ctx.fillText(label, node.x, node.y + radius + 3);
              }}
              linkColor={() => `rgba(143,175,217,${isDark ? "0.2" : "0.25"})`}
              linkLabel={(link: any) => link.type || ""}
              linkWidth={0.5}
              linkDirectionalArrowLength={3}
              linkDirectionalArrowRelPos={1}
              cooldownTicks={100}
              d3VelocityDecay={0.3}
              onNodeClick={(node: any) => setSelectedNode(node)}
              onNodeHover={(node: any) => setHoveredNode(node || null)}
              backgroundColor="transparent"
              width={undefined}
              height={undefined}
            />
          </Card>

          {/* Selected node info */}
          {selectedNode && (
            <Card sx={{ p: 2, mb: 2 }}>
              <Typography variant="subtitle2" gutterBottom>
                Selected: {selectedNode.name}
              </Typography>
              <Box sx={{ display: "flex", gap: 2, flexWrap: "wrap", alignItems: "center" }}>
                <Chip label={selectedNode.type} size="small" />
                <Typography variant="caption" color="text.secondary" sx={{ fontFamily: "monospace" }}>
                  ID: {selectedNode.id}
                </Typography>
                {selectedNode.summary && (
                  <Typography variant="body2" color="text.secondary">
                    {selectedNode.summary}
                  </Typography>
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
