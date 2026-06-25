"use client";

import { useEffect, useState, useRef, useCallback, useMemo } from "react";
import * as d3 from "d3";
import {
  Search,
  ZoomIn,
  ZoomOut,
  RotateCcw,
  GitBranch,
  Info,
  X,
  Maximize2,
  Minimize2,
} from "lucide-react";
import { Button } from "@/components/ui/button";

// ╔══════════════════════════════════════════════════════════════════════════════╗
// ║ Public Types                                                                ║
// ╚══════════════════════════════════════════════════════════════════════════════╝

export interface GraphNodeData {
  id: string;
  name: string;
  type: string;
  summary: string | null;
  created_at: string;
}

export interface GraphEdgeData {
  id: string;
  source_id: string;
  target_id: string;
  type: string;
}

export interface ApiConfig {
  baseUrl: string;
  projectId: string;
  headers: Record<string, string>;
}

// ╔══════════════════════════════════════════════════════════════════════════════╗
// ║ Internal Types                                                              ║
// ╚══════════════════════════════════════════════════════════════════════════════╝

/** D3 node — extends GraphNodeData with simulation position/velocity fields. */
interface D3Node extends GraphNodeData {
  x?: number;
  y?: number;
  fx?: number | null;
  fy?: number | null;
}

/** D3 link — source/target are string IDs until resolved by forceLink. */
interface D3Link {
  id: string;
  source: string;
  target: string;
  type: string;
}

interface NodeDetailResponse {
  data: {
    node: GraphNodeData & { metadata?: Record<string, unknown> };
    edges: GraphEdgeData[];
  };
}

// ╔══════════════════════════════════════════════════════════════════════════════╗
// ║ Constants & Helpers                                                         ║
// ╚══════════════════════════════════════════════════════════════════════════════╝

const NODE_COLORS: Record<string, string> = {
  person: "#14488C",
  organization: "#1453A6",
  location: "#8FAFD9",
  event: "#1747A6",
  concept: "#6A8DB8",
};
const DEFAULT_NODE_COLOR = "#4A6D96";

const ENTITY_TYPE_LEGEND = [
  { type: "person", label: "Person", color: NODE_COLORS.person },
  { type: "organization", label: "Organization", color: NODE_COLORS.organization },
  { type: "location", label: "Location", color: NODE_COLORS.location },
  { type: "event", label: "Event", color: NODE_COLORS.event },
  { type: "concept", label: "Concept", color: NODE_COLORS.concept },
];

function getColor(type: string | undefined): string {
  return NODE_COLORS[type?.toLowerCase() ?? ""] ?? DEFAULT_NODE_COLOR;
}

function timeAgo(dateStr: string): string {
  const seconds = Math.floor((Date.now() - new Date(dateStr).getTime()) / 1000);
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const mins = Math.floor(seconds / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

// ╔══════════════════════════════════════════════════════════════════════════════╗
// ║ Spinner                                                                     ║
// ╚══════════════════════════════════════════════════════════════════════════════╝

function Spinner({ className }: { className?: string }) {
  return (
    <svg
      className={`animate-spin h-5 w-5 ${className ?? ""}`}
      xmlns="http://www.w3.org/2000/svg"
      fill="none"
      viewBox="0 0 24 24"
    >
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path
        className="opacity-75"
        fill="currentColor"
        d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
      />
    </svg>
  );
}

// ╔══════════════════════════════════════════════════════════════════════════════╗
// ║ Props                                                                       ║
// ╚══════════════════════════════════════════════════════════════════════════════╝

export interface ForceGraphProps {
  nodes: GraphNodeData[];
  edges: GraphEdgeData[];
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;
  apiConfig: ApiConfig;
  userName?: string;
  showFilter?: boolean;
  showControls?: boolean;
  showLegend?: boolean;
  height?: number;
  emptyMessage?: string;
  emptyAction?: React.ReactNode;
}

// ╔══════════════════════════════════════════════════════════════════════════════╗
// ║ Component                                                                   ║
// ╚══════════════════════════════════════════════════════════════════════════════╝

export function ForceGraph({
  nodes: allNodes,
  edges: allEdges,
  loading = false,
  error = null,
  onRetry,
  apiConfig,
  userName,
  showFilter = true,
  showControls = true,
  showLegend = true,
  height = 600,
  emptyMessage = "No graph entities found",
  emptyAction,
}: ForceGraphProps) {
  // ── State ────────────────────────────────────────────────────────────────
  const [filterText, setFilterText] = useState("");
  const [showRelated, setShowRelated] = useState(true);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [selectedNode, setSelectedNode] = useState<GraphNodeData | null>(null);
  const [nodeDetail, setNodeDetail] = useState<{
    node: GraphNodeData & { metadata?: Record<string, unknown> };
    edges: GraphEdgeData[];
    loading: boolean;
  } | null>(null);

  // ── D3 refs ─────────────────────────────────────────────────────────────
  const containerRef = useRef<HTMLDivElement>(null);
  const simulationRef = useRef<d3.Simulation<D3Node, D3Link> | null>(null);

  // ── Derived: filtered data ─────────────────────────────────────────────
  const filteredData = useMemo<{ nodes: GraphNodeData[]; edges: GraphEdgeData[] } | null>(() => {
    if (allNodes.length === 0) return null;

    const lowerFilter = filterText.toLowerCase().trim();
    if (!lowerFilter) return { nodes: allNodes, edges: allEdges };

    // Nodes that match the search text
    const matchingNodes = allNodes.filter(
      (n) =>
        n.name.toLowerCase().includes(lowerFilter) ||
        n.type.toLowerCase().includes(lowerFilter),
    );
    const matchingNodeIds = new Set(matchingNodes.map((n) => n.id));

    if (!showRelated) {
      // Strict mode: only edges connecting two matching nodes
      const strictEdges = allEdges.filter(
        (l) => matchingNodeIds.has(l.source_id) && matchingNodeIds.has(l.target_id),
      );
      return { nodes: matchingNodes, edges: strictEdges };
    }

    // Related mode: include 1-hop neighbors of matching nodes
    const neighborIds = new Set<string>();
    for (const edge of allEdges) {
      if (matchingNodeIds.has(edge.source_id)) neighborIds.add(edge.target_id);
      if (matchingNodeIds.has(edge.target_id)) neighborIds.add(edge.source_id);
    }
    const expandedNodeIds = new Set([...matchingNodeIds, ...neighborIds]);
    const expandedNodes = allNodes.filter((n) => expandedNodeIds.has(n.id));
    const expandedEdges = allEdges.filter(
      (l) => expandedNodeIds.has(l.source_id) && expandedNodeIds.has(l.target_id),
    );

    return { nodes: expandedNodes, edges: expandedEdges };
  }, [allNodes, allEdges, filterText, showRelated]);

  // Clear selected node when filter hides it
  useEffect(() => {
    if (selectedNode && filteredData) {
      const stillVisible = filteredData.nodes.some((n) => n.id === selectedNode.id);
      if (!stillVisible) setSelectedNode(null);
    }
  }, [filteredData, selectedNode]);

  // Fullscreen: Escape to exit + lock body scroll
  useEffect(() => {
    if (!isFullscreen) return;
    document.body.style.overflow = "hidden";
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") setIsFullscreen(false);
    };
    window.addEventListener("keydown", handler);
    return () => {
      document.body.style.overflow = "";
      window.removeEventListener("keydown", handler);
    };
  }, [isFullscreen]);

  // ── Derived: connected edges & neighbors for selected node ─────────────
  const connectedEdges = useMemo<GraphEdgeData[]>(() => {
    if (!selectedNode) return [];
    return allEdges.filter(
      (e) => e.source_id === selectedNode.id || e.target_id === selectedNode.id,
    );
  }, [selectedNode, allEdges]);

  const connectedNodes = useMemo<GraphNodeData[]>(() => {
    if (!selectedNode) return [];
    const neighborIds = new Set<string>();
    for (const e of connectedEdges) {
      if (e.source_id === selectedNode.id) neighborIds.add(e.target_id);
      if (e.target_id === selectedNode.id) neighborIds.add(e.source_id);
    }
    return allNodes.filter((n) => neighborIds.has(n.id));
  }, [selectedNode, allNodes, connectedEdges]);

  // ── Fetch node detail when selected ────────────────────────────────────
  useEffect(() => {
    if (!selectedNode) {
      setNodeDetail(null);
      return;
    }

    let cancelled = false;
    setNodeDetail((prev) => (prev ? { ...prev, loading: true } : null));

    (async () => {
      try {
        const res = await fetch(
          `${apiConfig.baseUrl}/v1/projects/${apiConfig.projectId}/graph/nodes/${selectedNode.id}`,
          { headers: apiConfig.headers },
        );
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const json: NodeDetailResponse = await res.json();
        const detail = json.data;
        if (!cancelled) {
          setNodeDetail({
            node: detail.node ?? selectedNode,
            edges: detail.edges ?? [],
            loading: false,
          });
        }
      } catch {
        if (!cancelled) {
          // Fallback: use local data
          const localEdges = allEdges.filter(
            (e) => e.source_id === selectedNode.id || e.target_id === selectedNode.id,
          );
          setNodeDetail({
            node: selectedNode,
            edges: localEdges,
            loading: false,
          });
        }
      }
    })();

    return () => { cancelled = true; };
  }, [selectedNode, apiConfig.baseUrl, apiConfig.projectId, apiConfig.headers, allEdges]);

  // ╔══════════════════════════════════════════════════════════════════════╗
  // ║ D3 Force Graph Rendering                                            ║
  // ╚══════════════════════════════════════════════════════════════════════╝

  useEffect(() => {
    const container = containerRef.current;
    if (!container || !filteredData || filteredData.nodes.length === 0) return;

    // ── Measure ──────────────────────────────────────────────────────────
    const width = container.clientWidth;

    // ── Clear previous render ─────────────────────────────────────────────
    d3.select(container).selectAll("svg, .d3-overlay").remove();

    // ── SVG root ──────────────────────────────────────────────────────────
    const svg = d3
      .select(container)
      .append("svg")
      .attr("width", width)
      .attr("height", height)
      .style("display", "block")
      .style("cursor", "grab");

    // ── Defs: drop shadow for nodes ──────────────────────────────────────
    const defs = svg.append("defs");
    const filter = defs
      .append("filter")
      .attr("id", "node-glow")
      .attr("x", "-50%")
      .attr("y", "-50%")
      .attr("width", "200%")
      .attr("height", "200%");
    filter
      .append("feDropShadow")
      .attr("dx", 0)
      .attr("dy", 0)
      .attr("stdDeviation", 3)
      .attr("flood-color", "rgba(20, 72, 140, 0.3)");

    // ── Main group for zoom/pan ───────────────────────────────────────────
    const g = svg.append("g");

    // ── Zoom behaviour ──────────────────────────────────────────────────
    const zoom = d3
      .zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.15, 5])
      .on("zoom", (event: d3.D3ZoomEvent<SVGSVGElement, unknown>) => {
        g.attr("transform", event.transform.toString());
      });

    svg.call(zoom);

    // ── Data conversion ────────────────────────────────────────────────
    const nodes: D3Node[] = filteredData.nodes.map((n) => ({ ...n }));
    const links: D3Link[] = filteredData.edges.map((l) => ({
      id: l.id,
      source: l.source_id,
      target: l.target_id,
      type: l.type,
    }));

    // ── Force simulation ──────────────────────────────────────────────
    const simulation = d3
      .forceSimulation<D3Node>(nodes)
      .force(
        "link",
        d3
          .forceLink<D3Node, D3Link>(links)
          .id((d) => d.id)
          .distance(120),
      )
      .force("charge", d3.forceManyBody().strength(-250))
      .force("center", d3.forceCenter(width / 2, height / 2))
      .force("collide", d3.forceCollide(32));

    simulationRef.current = simulation;

    // ── Edge layer ────────────────────────────────────────────────────
    const linkGroup = g.append("g").attr("class", "links");

    const link = linkGroup
      .selectAll<SVGLineElement, D3Link & { source: D3Node; target: D3Node }>("line")
      .data(links, (d) => d.id)
      .join("line")
      .attr("stroke", "rgba(143,175,217,0.25)")
      .attr("stroke-width", 1.5)
      .attr("stroke-linecap", "round");

    // ── Edge label layer ───────────────────────────────────────────────
    const linkLabelGroup = g.append("g").attr("class", "link-labels");

    const linkLabel = linkLabelGroup
      .selectAll<SVGTextElement, D3Link & { source: D3Node; target: D3Node }>("text")
      .data(links, (d) => d.id)
      .join("text")
      .text((d) => d.type)
      .attr("font-size", 9)
      .attr("font-family", "JetBrains Mono, monospace")
      .attr("fill", "rgba(143,175,217,0.4)")
      .attr("text-anchor", "middle")
      .attr("pointer-events", "none");

    // ── Node layer ────────────────────────────────────────────────────
    const nodeGroup = g.append("g").attr("class", "nodes");

    const node = nodeGroup
      .selectAll<SVGGElement, D3Node>("g")
      .data(nodes, (d) => d.id)
      .join("g")
      .attr("cursor", "pointer");

    // Circle
    node
      .append("circle")
      .attr("r", 8)
      .attr("fill", (d) => getColor(d.type))
      .attr("stroke", "rgba(255,255,255,0.08)")
      .attr("stroke-width", 1.5)
      .attr("filter", "url(#node-glow)");

    // Label
    node
      .append("text")
      .text((d) => d.name)
      .attr("dy", 20)
      .attr("text-anchor", "middle")
      .attr("font-size", 11)
      .attr("font-family", "Inter, sans-serif")
      .attr("fill", "#d1d5db")
      .attr("pointer-events", "none")
      .text((d) => (d.name.length > 20 ? `${d.name.slice(0, 18)}…` : d.name));

    // ── Drag behaviour ────────────────────────────────────────────────
    let dragMoved = false;

    const drag = d3
      .drag<SVGGElement, D3Node>()
      .on("start", (event: d3.D3DragEvent<SVGGElement, D3Node, D3Node>, d) => {
        dragMoved = false;
        if (!event.active) simulation.alphaTarget(0.3).restart();
        d.fx = d.x;
        d.fy = d.y;
      })
      .on("drag", (event: d3.D3DragEvent<SVGGElement, D3Node, D3Node>, d) => {
        dragMoved = true;
        d.fx = event.x;
        d.fy = event.y;
      })
      .on("end", (event: d3.D3DragEvent<SVGGElement, D3Node, D3Node>, d) => {
        if (!event.active) simulation.alphaTarget(0);
        d.fx = null;
        d.fy = null;
      });

    node.call(drag);

    // ── Click: select node (skip if it was a drag) ─────────────────────
    node.on("click", (_event: MouseEvent, d: D3Node) => {
      if (dragMoved) return;
      setSelectedNode(d);
    });

    // ── Hover: highlight connected edges ─────────────────────────────
    node
      .on("mouseenter", (_event: MouseEvent, d: D3Node) => {
        const connectedIds = new Set<string>();
        connectedIds.add(d.id);

        link
          .attr("stroke", (l) => {
            const sourceObj = l.source as unknown as D3Node;
            const targetObj = l.target as unknown as D3Node;
            const sid = typeof l.source === "object" ? sourceObj.id : l.source;
            const tid = typeof l.target === "object" ? targetObj.id : l.target;
            const isConnected = sid === d.id || tid === d.id;
            if (isConnected) {
              connectedIds.add(sid);
              connectedIds.add(tid);
            }
            return isConnected ? "rgba(143,175,217,0.6)" : "rgba(143,175,217,0.04)";
          })
          .attr("stroke-width", (l) => {
            const sourceObj = l.source as unknown as D3Node;
            const targetObj = l.target as unknown as D3Node;
            const sid = typeof l.source === "object" ? sourceObj.id : l.source;
            const tid = typeof l.target === "object" ? targetObj.id : l.target;
            return sid === d.id || tid === d.id ? 2.5 : 1;
          });

        node.attr("opacity", (n) => (connectedIds.has(n.id) ? 1 : 0.2));
        linkLabel.attr("opacity", (l) => {
          const sourceObj = l.source as unknown as D3Node;
          const targetObj = l.target as unknown as D3Node;
          const sid = typeof l.source === "object" ? sourceObj.id : l.source;
          const tid = typeof l.target === "object" ? targetObj.id : l.target;
          return sid === d.id || tid === d.id ? 1 : 0.05;
        });
      })
      .on("mouseleave", () => {
        link
          .attr("stroke", "rgba(143,175,217,0.25)")
          .attr("stroke-width", 1.5);
        node.attr("opacity", 1);
        linkLabel.attr("opacity", 1);
      });

    // ── Tick handler ──────────────────────────────────────────────────
    simulation.on("tick", () => {
      link
        .attr("x1", (d) => (d.source as unknown as D3Node).x!)
        .attr("y1", (d) => (d.source as unknown as D3Node).y!)
        .attr("x2", (d) => (d.target as unknown as D3Node).x!)
        .attr("y2", (d) => (d.target as unknown as D3Node).y!);

      linkLabel
        .attr("x", (d) => ((d.source as unknown as D3Node).x! + (d.target as unknown as D3Node).x!) / 2)
        .attr("y", (d) => ((d.source as unknown as D3Node).y! + (d.target as unknown as D3Node).y!) / 2);

      node.attr("transform", (d) => `translate(${d.x},${d.y})`);
    });

    // ── Zoom-to-fit on first render ──────────────────────────────────
    const fitTimer = setTimeout(() => {
      if (nodes.length === 0) return;
      const bounds = (g.node() as SVGGElement)?.getBBox();
      if (!bounds || bounds.width === 0 || bounds.height === 0) return;

      const padding = 60;
      const scale = Math.min(
        width / (bounds.width + padding * 2),
        height / (bounds.height + padding * 2),
        2,
      );
      const tx = width / 2 - (bounds.x + bounds.width / 2) * scale;
      const ty = height / 2 - (bounds.y + bounds.height / 2) * scale;

      svg
        .transition()
        .duration(500)
        .call(
          zoom.transform,
          d3.zoomIdentity.translate(tx, ty).scale(scale),
        );
    }, 100);

    // ── Cleanup ──────────────────────────────────────────────────────
    return () => {
      clearTimeout(fitTimer);
      simulation.stop();
      simulationRef.current = null;
      d3.select(container).selectAll("svg, .d3-overlay").remove();
    };
  }, [filteredData, height]);

  // ╔══════════════════════════════════════════════════════════════════════╗
  // ║ Zoom controls                                                       ║
  // ╚══════════════════════════════════════════════════════════════════════╝

  const handleZoomIn = useCallback(() => {
    const svgEl = containerRef.current?.querySelector("svg");
    if (!svgEl) return;
    const selection = d3.select(svgEl);
    selection.transition().duration(200).call(d3.zoom<SVGSVGElement, unknown>().scaleBy, 1.3);
  }, []);

  const handleZoomOut = useCallback(() => {
    const svgEl = containerRef.current?.querySelector("svg");
    if (!svgEl) return;
    const selection = d3.select(svgEl);
    selection.transition().duration(200).call(d3.zoom<SVGSVGElement, unknown>().scaleBy, 0.7);
  }, []);

  const handleResetZoom = useCallback(() => {
    const svgEl = containerRef.current?.querySelector("svg");
    if (!svgEl) return;
    const selection = d3.select(svgEl);
    selection
      .transition()
      .duration(400)
      .call(
        d3.zoom<SVGSVGElement, unknown>().transform,
        d3.zoomIdentity,
      );
  }, []);

  // ╔══════════════════════════════════════════════════════════════════════╗
  // ║ Derived counts                                                      ║
  // ╚══════════════════════════════════════════════════════════════════════╝

  const nodeCount = allNodes.length;
  const edgeCount = allEdges.length;
  const filteredNodeCount = filteredData?.nodes.length ?? 0;
  const filteredEdgeCount = filteredData?.edges.length ?? 0;
  const isFiltered = filterText.trim().length > 0;

  // ╔══════════════════════════════════════════════════════════════════════╗
  // ║ Render                                                              ║
  // ╚══════════════════════════════════════════════════════════════════════╝

  return (
    <div className={`space-y-4 ${isFullscreen ? 'fixed inset-0 z-50 bg-surface-950 p-6 overflow-y-auto' : ''}`}>
      {/* ── Control bar ───────────────────────────────────────────────── */}
      {(showFilter || showControls) && (
        <div className="flex items-center gap-3 flex-wrap">
          {showFilter && (
            <div className="relative flex-1 min-w-[200px] max-w-sm">
              <Search
                size={14}
                className="absolute left-3 top-1/2 -translate-y-1/2 text-surface-500 pointer-events-none"
              />
              <input
                className="input-base pl-9 pr-3 text-sm"
                placeholder="Filter by name or type…"
                value={filterText}
                onChange={(e) => setFilterText(e.target.value)}
              />
            </div>
          )}

          {/* Search mode: segmented control */}
          {showFilter && (
            <div className="flex items-center rounded-md border border-surface-700 overflow-hidden shrink-0">
              <button
                onClick={() => setShowRelated(false)}
                title="Show only matching nodes (strict)"
                className={`px-2.5 py-1 text-xs font-medium transition-colors ${
                  !showRelated
                    ? "bg-surface-800 text-white"
                    : "text-surface-400 hover:text-surface-200"
                }`}
              >
                Exact
              </button>
              <div className="w-px h-3 bg-surface-700" aria-hidden="true" />
              <button
                onClick={() => setShowRelated(true)}
                title="Include 1-hop neighbors of matched nodes"
                className={`px-2.5 py-1 text-xs font-medium transition-colors ${
                  showRelated
                    ? "bg-surface-800 text-white"
                    : "text-surface-400 hover:text-surface-200"
                }`}
              >
                Related
              </button>
            </div>
          )}

          {/* Stats */}
          <div className="flex items-center gap-4 text-xs text-surface-400 ml-auto">
            <span className="flex items-center gap-1.5">
              <GitBranch size={14} className="text-accent-300/60" />
              {isFiltered ? `${filteredNodeCount} / ` : ""}
              {nodeCount} node{nodeCount !== 1 ? "s" : ""}
            </span>
            <span className="flex items-center gap-1.5">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" className="text-accent-300/60">
                <path
                  d="M5 8L12 3L19 8L12 13L5 8Z"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  fill="none"
                />
                <path
                  d="M5 16L12 21L19 16"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  fill="none"
                />
              </svg>
              {isFiltered ? `${filteredEdgeCount} / ` : ""}
              {edgeCount} edge{edgeCount !== 1 ? "s" : ""}
            </span>
          </div>

          {showControls && (
            <div className="flex items-center gap-1 border-l border-surface-700 pl-3">
              <Button
                variant="ghost"
                size="sm"
                onClick={handleZoomIn}
                className="rounded-md text-surface-400 hover:text-white"
                title="Zoom in"
              >
                <ZoomIn size={16} />
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={handleZoomOut}
                className="rounded-md text-surface-400 hover:text-white"
                title="Zoom out"
              >
                <ZoomOut size={16} />
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={handleResetZoom}
                className="rounded-md text-surface-400 hover:text-white"
                title="Reset zoom"
              >
                <RotateCcw size={14} />
              </Button>
              <div className="w-px h-4 bg-surface-700" aria-hidden="true" />
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setIsFullscreen((p) => !p)}
                className="rounded-md text-surface-400 hover:text-white"
                title={isFullscreen ? "Exit fullscreen (Esc)" : "Enter fullscreen"}
              >
                {isFullscreen ? <Minimize2 size={16} /> : <Maximize2 size={16} />}
              </Button>
            </div>
          )}
        </div>
      )}

      {/* ── Graph area ────────────────────────────────────────────────── */}
      <div className="card-base overflow-hidden">
        <div
          ref={containerRef}
          className="relative"
          style={{ height: isFullscreen ? 'calc(100vh - 130px)' : `${height}px` }}
        >
          {/* Loading overlay */}
          {loading && (
            <div className="absolute inset-0 z-10 flex flex-col items-center justify-center bg-surface-900/80">
              <Spinner className="text-accent-300 h-8 w-8" />
              <p className="text-sm text-surface-400 mt-3">Loading graph data…</p>
            </div>
          )}

          {/* Error state */}
          {error && !loading && (
            <div className="absolute inset-0 z-10 flex flex-col items-center justify-center">
              <div className="flex h-12 w-12 items-center justify-center rounded-full bg-error/10 mb-3">
                <X size={22} className="text-error" />
              </div>
              <p className="text-sm text-surface-300 font-medium">{error}</p>
              <p className="text-xs text-surface-500 mt-1 mb-4">
                Check your connection and try again
              </p>
              {onRetry && (
                <Button variant="primary" size="sm" onClick={onRetry}>
                  Retry
                </Button>
              )}
            </div>
          )}

          {/* Empty state */}
          {!loading && !error && filteredData && filteredData.nodes.length === 0 && (
            <div className="absolute inset-0 z-10 flex flex-col items-center justify-center">
              <div className="flex h-12 w-12 items-center justify-center rounded-full bg-surface-800 mb-3">
                <Info size={22} className="text-surface-500" />
              </div>
              <p className="text-sm text-surface-300 font-medium">
                {isFiltered ? "No matching entities" : emptyMessage}
              </p>
              <p className="text-xs text-surface-500 mt-1">
                {isFiltered
                  ? "Try a different filter term"
                  : "Ingest some data to populate the knowledge graph"}
              </p>
              {isFiltered && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setFilterText("")}
                  className="text-accent-300 mt-3"
                >
                  Clear filter
                </Button>
              )}
              {!isFiltered && emptyAction}
            </div>
          )}

          {/* D3 renders into the SVG inside this container */}

          {/* ── Floating node info panel ──────────────────────────────── */}
          {selectedNode && (
            <div className="absolute bottom-4 right-4 z-20 w-80 max-h-[calc(100%-2rem)] overflow-y-auto rounded-lg border border-surface-700/50 bg-surface-900/95 backdrop-blur-md p-4 shadow-xl animate-fade-in">
              {nodeDetail?.loading && (
                <div className="flex items-center justify-center py-8">
                  <Spinner className="text-accent-300 h-5 w-5" />
                </div>
              )}

              {(!nodeDetail || !nodeDetail.loading) && (
                <>
                  {/* Header */}
                  <div className="flex items-start justify-between gap-2 mb-3">
                    <div className="flex items-center gap-2 min-w-0">
                      <span
                        className="mt-0.5 block h-3 w-3 shrink-0 rounded-full"
                        style={{ backgroundColor: getColor(selectedNode.type) }}
                      />
                      <div className="min-w-0">
                        <h3 className="text-sm font-semibold text-white truncate">
                          {selectedNode.name}
                        </h3>
                      </div>
                      <span
                        className="text-[10px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded shrink-0"
                        style={{
                          backgroundColor: `${getColor(selectedNode.type)}20`,
                          color: getColor(selectedNode.type),
                        }}
                      >
                        {selectedNode.type}
                      </span>
                    </div>
                    <Button
                      variant="ghost"
                      onClick={() => setSelectedNode(null)}
                      className="rounded text-surface-500 hover:text-white shrink-0"
                    >
                      <X size={14} />
                    </Button>
                  </div>

                  {/* Summary */}
                  {selectedNode.summary && (
                    <p className="text-xs text-surface-400 mb-2 leading-relaxed">
                      {selectedNode.summary}
                    </p>
                  )}

                  {/* Metadata from API */}
                  {nodeDetail?.node?.metadata && Object.keys(nodeDetail.node.metadata).length > 0 && (
                    <div className="mt-2 pt-2 border-t border-surface-800">
                      <h4 className="text-[10px] font-medium text-surface-500 mb-1 uppercase tracking-wider">Metadata</h4>
                      <div className="space-y-0.5">
                        {Object.entries(nodeDetail.node.metadata).map(([key, val]) => (
                          <div key={key} className="flex gap-2 text-[11px]">
                            <span className="text-surface-500 shrink-0">{key}:</span>
                            <span className="text-surface-300 truncate">
                              {typeof val === "object" ? JSON.stringify(val) : String(val)}
                            </span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* Relationships */}
                  {(nodeDetail?.edges ?? connectedEdges).length > 0 && (
                    <div className="mt-2 pt-2 border-t border-surface-800">
                      <h4 className="text-[10px] font-medium text-surface-500 mb-1.5 uppercase tracking-wider">
                        Relationships ({(nodeDetail?.edges ?? connectedEdges).length})
                      </h4>
                      <div className="space-y-1">
                        {(nodeDetail?.edges ?? connectedEdges).map((edge, i) => {
                          const isSource = edge.source_id === selectedNode.id;
                          const neighborId = isSource ? edge.target_id : edge.source_id;
                          const neighbor = allNodes.find((n) => n.id === neighborId);
                          return (
                            <div key={i} className="flex items-center gap-1.5 text-[11px]">
                              <span className="text-surface-600 shrink-0">
                                {isSource ? "→" : "←"}
                              </span>
                              <span className="font-medium text-accent-300/80">{edge.type}</span>
                              <span className="text-surface-600">→</span>
                              {neighbor ? (
                                <button
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    setSelectedNode(neighbor);
                                  }}
                                  className="truncate text-surface-200 hover:text-accent-300 underline underline-offset-2 decoration-surface-700 hover:decoration-accent-300 transition-colors"
                                >
                                  {neighbor.name}
                                </button>
                              ) : (
                                <span className="text-surface-500 font-mono truncate">
                                  {neighborId.slice(0, 8)}
                                </span>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  )}

                  {/* ID + created */}
                  <div className="mt-2 pt-2 border-t border-surface-800 flex items-center justify-between text-[10px] text-surface-600">
                    <span className="font-mono truncate max-w-[140px]" title={selectedNode.id}>
                      {selectedNode.id.slice(0, 12)}…
                    </span>
                    <span>Created {timeAgo(selectedNode.created_at)}</span>
                  </div>
                </>
              )}
            </div>
          )}
        </div>
      </div>

      {/* ── Legend ────────────────────────────────────────────────────── */}
      {!isFullscreen && showLegend && allNodes.length > 0 && (
        <div className="card-base p-3">
          <div className="flex items-center gap-6 flex-wrap">
            <span className="text-xs text-surface-500 font-medium uppercase tracking-wider">
              Entity Types
            </span>
            {ENTITY_TYPE_LEGEND.map((entry) => (
              <div key={entry.type} className="flex items-center gap-2">
                <span
                  className="block h-2.5 w-2.5 rounded-full shrink-0"
                  style={{ backgroundColor: entry.color }}
                />
                <span className="text-xs text-surface-400">{entry.label}</span>
              </div>
            ))}
            <div className="flex items-center gap-2">
              <span
                className="block h-2.5 w-2.5 rounded-full shrink-0"
                style={{ backgroundColor: DEFAULT_NODE_COLOR }}
              />
              <span className="text-xs text-surface-400">Other</span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
