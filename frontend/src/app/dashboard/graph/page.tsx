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
  useTheme,
} from "@mui/material";
import { alpha } from "@mui/material/styles";
import ForceGraph2D from "react-force-graph-2d";
import { listGraphNodes, listGraphEdges } from "@/lib/api/client";
import { useAuth } from "@/lib/auth/useAuth";
import { listUsers } from "@/lib/api/client";
import PageHeader from "@/components/shared/PageHeader";

// ─── Types ────────────────────────────────────────────────────────────────────

interface GraphNode {
  id: string;
  name: string;
  type: string;
  summary: string | null;
  val?: number;
}

interface GraphLink {
  source: string;
  target: string;
  type: string;
}

interface EdgeData {
  id: string;
  source_id: string;
  target_id: string;
  type: string;
}

// ─── Colors ───────────────────────────────────────────────────────────────────

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

// ─── Page Component ───────────────────────────────────────────────────────────

export default function GraphPage() {
  useAuth();
  const theme = useTheme();
  const fgRef = useRef<any>(null);

  const [nodes, setNodes] = useState<GraphNode[]>([]);
  const [links, setLinks] = useState<GraphLink[]>([]);
  const [loading, setLoading] = useState(true);
  const [entityFilter, setEntityFilter] = useState("");
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
  const [hoveredNode, setHoveredNode] = useState<GraphNode | null>(null);
  const [userId, setUserId] = useState<string>("");

  // ── Fetch users for selector ────────────────────────────────────────────────

  useEffect(() => {
    listUsers({ limit: 1 })
      .then((result) => {
        const users = (result.data as { id: string; external_id: string }[]) ?? [];
        if (users.length > 0) setUserId(users[0].id);
      })
      .catch(() => {});
  }, []);

  // ── Fetch graph data ────────────────────────────────────────────────────────

  const fetchGraph = useCallback(async () => {
    if (!userId) return;
    setLoading(true);
    setSelectedNode(null);
    try {
      const params: Record<string, string | number | boolean | undefined | null> = {
        limit: 100,
      };
      if (entityFilter) params.entity_type = entityFilter;

      const result = await listGraphNodes(userId, params);
      const nodeList = ((result.data as any)?.items ?? []) as any[];

      // Fetch edges for all nodes
      const allEdges: EdgeData[] = [];
      const seen = new Set<string>();
      const batchSize = 5;
      for (let i = 0; i < nodeList.length; i += batchSize) {
        const batch = nodeList.slice(i, i + batchSize);
        const edgeResults = await Promise.allSettled(
          batch.map((n) =>
            listGraphEdges(userId, { subject_id: n.id, limit: 50 }).catch(
              () => ({ data: { items: [] } } as any),
            ),
          ),
        );
        for (const r of edgeResults) {
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
      const fgNodes: GraphNode[] = nodeList.map((n: any) => ({
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

  return (
    <Box>
      <PageHeader
        title="Knowledge Graph"
        subtitle="Explore entities and relationships extracted from your data"
      />

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
          {nodes.length} entities · {links.length} relationships
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
          {/* Force graph */}
          <Card
            sx={{
              width: "100%",
              height: 500,
              overflow: "hidden",
              mb: 2,
              position: "relative",
              bgcolor: "background.default",
            }}
          >
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
        </>
      )}
    </Box>
  );
}
