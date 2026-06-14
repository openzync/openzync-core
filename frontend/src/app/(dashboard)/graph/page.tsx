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
} from "lucide-react";

// ╔══════════════════════════════════════════════════════════════════════════════╗
// ║ Types                                                                       ║
// ╚══════════════════════════════════════════════════════════════════════════════╝

interface GraphNode {
  id: string;
  name: string;
  type: string;
  summary: string | null;
  created_at: string;
}

interface GraphEdge {
  id: string;
  source_id: string;
  target_id: string;
  type: string;
}

interface GraphData {
  nodes: GraphNode[];
  links: GraphEdge[];
}

/** D3 node — extends GraphNode with simulation position/velocity fields. */
interface D3Node extends GraphNode {
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

interface NodesApiResponse {
  data: {
    items: GraphNode[];
    has_more: boolean;
  };
}

interface EdgesApiResponse {
  data: {
    items: GraphEdge[];
  };
}

interface UsersApiResponse {
  data: { id: string; name: string | null }[];
}

// ╔══════════════════════════════════════════════════════════════════════════════╗
// ║ Constants & Helpers                                                         ║
// ╚══════════════════════════════════════════════════════════════════════════════╝

const API_BASE = "http://localhost:8000";

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

function authHeaders(): Record<string, string> {
  const token = sessionStorage.getItem("mg_access_token");
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  return headers;
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
// ║ Main Page                                                                   ║
// ╚══════════════════════════════════════════════════════════════════════════════╝

export default function GraphExplorerPage() {
  // ── Data state ────────────────────────────────────────────────────────────
  const [graphData, setGraphData] = useState<GraphData | null>(null);
  const [loading, setLoading] = useState(true);
  const [hasError, setHasError] = useState(false);
  const [filterText, setFilterText] = useState("");
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
  const [userName, setUserName] = useState<string>("");

  // ── D3 refs ───────────────────────────────────────────────────────────────
  const containerRef = useRef<HTMLDivElement>(null);
  const simulationRef = useRef<d3.Simulation<D3Node, D3Link> | null>(null);

  // ── Derived: filtered data ────────────────────────────────────────────────
  const filteredData = useMemo<GraphData | null>(() => {
    if (!graphData) return null;

    const lowerFilter = filterText.toLowerCase().trim();
    if (!lowerFilter) return graphData;

    const filteredNodes = graphData.nodes.filter(
      (n) =>
        n.name.toLowerCase().includes(lowerFilter) ||
        n.type.toLowerCase().includes(lowerFilter),
    );
    const filteredNodeIds = new Set(filteredNodes.map((n) => n.id));
    const filteredLinks = graphData.links.filter(
      (l) => filteredNodeIds.has(l.source_id) && filteredNodeIds.has(l.target_id),
    );

    return { nodes: filteredNodes, links: filteredLinks };
  }, [graphData, filterText]);

  // Clear selected node when filter hides it
  useEffect(() => {
    if (selectedNode && filteredData) {
      const stillVisible = filteredData.nodes.some((n) => n.id === selectedNode.id);
      if (!stillVisible) setSelectedNode(null);
    }
  }, [filteredData, selectedNode]);

  // ╔══════════════════════════════════════════════════════════════════════════╗
  // ║ Data Loading                                                            ║
  // ╚══════════════════════════════════════════════════════════════════════════╝

  const loadData = useCallback(async () => {
    setLoading(true);
    setHasError(false);
    setSelectedNode(null);
    setFilterText("");

    try {
      // Step 1: fetch first user
      const usersRes = await fetch(`${API_BASE}/v1/users?limit=1`, {
        headers: authHeaders(),
      });
      if (!usersRes.ok) throw new Error(`Failed to fetch users: ${usersRes.status}`);
      const usersData: UsersApiResponse = await usersRes.json();
      const user = usersData.data?.[0];
      if (!user) {
        setGraphData({ nodes: [], links: [] });
        setLoading(false);
        return;
      }
      setUserName(user.name ?? user.id);

      // Step 2: fetch nodes for the user
      const nodesRes = await fetch(
        `${API_BASE}/v1/users/${user.id}/graph/nodes?limit=100`,
        { headers: authHeaders() },
      );
      if (!nodesRes.ok) throw new Error(`Failed to fetch nodes: ${nodesRes.status}`);
      const nodesData: NodesApiResponse = await nodesRes.json();
      const nodes: GraphNode[] = nodesData.data?.items ?? [];

      if (nodes.length === 0) {
        setGraphData({ nodes: [], links: [] });
        setLoading(false);
        return;
      }

      // Step 3: fetch edges for each node in batches of 5
      const allEdges: GraphEdge[] = [];
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
              // Skip self-loops
              if (edge.source_id === edge.target_id) continue;
              // Deduplicate: undirected edge dedup using sorted pair
              const key = [edge.source_id, edge.target_id].sort().join("::");
              if (seen.has(key)) continue;
              seen.add(key);
              allEdges.push(edge);
            }
          }
        }
      }

      setGraphData({ nodes, links: allEdges });
    } catch {
      setHasError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  // ╔══════════════════════════════════════════════════════════════════════════╗
  // ║ D3 Force Graph Rendering                                                ║
  // ╚══════════════════════════════════════════════════════════════════════════╝

  useEffect(() => {
    const container = containerRef.current;
    if (!container || !filteredData || filteredData.nodes.length === 0) return;

    // ── Measure ────────────────────────────────────────────────────────────
    const width = container.clientWidth;
    const height = 600;

    // ── Clear previous render ───────────────────────────────────────────────
    d3.select(container).selectAll("svg, .d3-overlay").remove();

    // ── SVG root ────────────────────────────────────────────────────────────
    const svg = d3
      .select(container)
      .append("svg")
      .attr("width", width)
      .attr("height", height)
      .style("display", "block")
      .style("cursor", "grab");

    // ── Defs: drop shadow for nodes ────────────────────────────────────────
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

    // ── Main group for zoom/pan ─────────────────────────────────────────────
    const g = svg.append("g");

    // ── Zoom behaviour ────────────────────────────────────────────────────
    const zoom = d3
      .zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.15, 5])
      .on("zoom", (event: d3.D3ZoomEvent<SVGSVGElement, unknown>) => {
        g.attr("transform", event.transform.toString());
      });

    svg.call(zoom);

    // ── Data conversion ──────────────────────────────────────────────────
    const nodes: D3Node[] = filteredData.nodes.map((n) => ({ ...n }));
    const links: D3Link[] = filteredData.links.map((l) => ({
      id: l.id,
      source: l.source_id,
      target: l.target_id,
      type: l.type,
    }));

    // ── Force simulation ────────────────────────────────────────────────
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

    // ── Edge layer (rendered first, behind nodes) ─────────────────────────
    const linkGroup = g.append("g").attr("class", "links");

    const link = linkGroup
      .selectAll<SVGLineElement, D3Link & { source: D3Node; target: D3Node }>("line")
      .data(links, (d) => d.id)
      .join("line")
      .attr("stroke", "rgba(143,175,217,0.25)")
      .attr("stroke-width", 1.5)
      .attr("stroke-linecap", "round");

    // ── Edge label layer ─────────────────────────────────────────────────
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

    // ── Node layer ───────────────────────────────────────────────────────
    const nodeGroup = g.append("g").attr("class", "nodes");

    // Outer group for each node (circle + label)
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
      // Truncate long names
      .text((d) => (d.name.length > 20 ? `${d.name.slice(0, 18)}…` : d.name));

    // ── Drag behaviour ───────────────────────────────────────────────────
    const drag = d3
      .drag<SVGGElement, D3Node>()
      .on("start", (event: d3.D3DragEvent<SVGGElement, D3Node, D3Node>, d) => {
        if (!event.active) simulation.alphaTarget(0.3).restart();
        d.fx = d.x;
        d.fy = d.y;
      })
      .on("drag", (event: d3.D3DragEvent<SVGGElement, D3Node, D3Node>, d) => {
        d.fx = event.x;
        d.fy = event.y;
      })
      .on("end", (event: d3.D3DragEvent<SVGGElement, D3Node, D3Node>, d) => {
        if (!event.active) simulation.alphaTarget(0);
        d.fx = null;
        d.fy = null;
      });

    node.call(drag);

    // ── Click: select node ────────────────────────────────────────────────
    node.on("click", (_event: MouseEvent, d: D3Node) => {
      setSelectedNode(d);
    });

    // ── Hover: highlight connected edges ──────────────────────────────────
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

    // ── Tick handler ────────────────────────────────────────────────────
    simulation.on("tick", () => {
      // After forceLink resolves, source/target are node objects (not string IDs)
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

    // ── Zoom-to-fit on first render ──────────────────────────────────────
    // Wait one tick for the simulation to settle a bit, then fit
    const fitTimer = setTimeout(() => {
      if (nodes.length === 0) return;
      const bounds = (g.node() as SVGGElement)?.getBBox();
      if (!bounds || bounds.width === 0 || bounds.height === 0) return;

      const padding = 60;
      const scale = Math.min(
        width / (bounds.width + padding * 2),
        height / (bounds.height + padding * 2),
        2, // cap zoom-in
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

    // ── Cleanup ──────────────────────────────────────────────────────────
    return () => {
      clearTimeout(fitTimer);
      simulation.stop();
      simulationRef.current = null;
      d3.select(container).selectAll("svg, .d3-overlay").remove();
    };
  }, [filteredData]);

  // ╔══════════════════════════════════════════════════════════════════════════╗
  // ║ Zoom controls                                                           ║
  // ╚══════════════════════════════════════════════════════════════════════════╝

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

  // ╔══════════════════════════════════════════════════════════════════════════╗
  // ║ Render                                                                  ║
  // ╚══════════════════════════════════════════════════════════════════════════╝

  const nodeCount = graphData?.nodes.length ?? 0;
  const edgeCount = graphData?.links.length ?? 0;
  const filteredNodeCount = filteredData?.nodes.length ?? 0;
  const filteredEdgeCount = filteredData?.links.length ?? 0;
  const isFiltered = filterText.trim().length > 0;

  return (
    <div className="space-y-6">
      {/* ── Page header ─────────────────────────────────────────────────── */}
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

      {/* ── Control bar ─────────────────────────────────────────────────── */}
      <div className="flex items-center gap-3 flex-wrap">
        {/* Search filter */}
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

        {/* Refresh */}
        <button
          onClick={loadData}
          disabled={loading}
          className="btn-secondary text-sm gap-2"
        >
          <RotateCcw size={14} className={loading ? "animate-spin" : ""} />
          Refresh
        </button>

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

        {/* Zoom controls */}
        <div className="flex items-center gap-1 border-l border-surface-700 pl-3">
          <button
            onClick={handleZoomIn}
            className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-white"
            title="Zoom in"
          >
            <ZoomIn size={16} />
          </button>
          <button
            onClick={handleZoomOut}
            className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-white"
            title="Zoom out"
          >
            <ZoomOut size={16} />
          </button>
          <button
            onClick={handleResetZoom}
            className="btn-ghost p-1.5 rounded-md text-surface-400 hover:text-white"
            title="Reset zoom"
          >
            <RotateCcw size={14} />
          </button>
        </div>
      </div>

      {/* ── Graph area ──────────────────────────────────────────────────── */}
      <div className="card-base overflow-hidden">
        <div
          ref={containerRef}
          className="relative"
          style={{ height: "600px" }}
        >
          {/* Loading overlay */}
          {loading && (
            <div className="absolute inset-0 z-10 flex flex-col items-center justify-center bg-surface-900/80">
              <Spinner className="text-accent-300 h-8 w-8" />
              <p className="text-sm text-surface-400 mt-3">Loading graph data…</p>
            </div>
          )}

          {/* Error state */}
          {hasError && !loading && (
            <div className="absolute inset-0 z-10 flex flex-col items-center justify-center">
              <div className="flex h-12 w-12 items-center justify-center rounded-full bg-error/10 mb-3">
                <X size={22} className="text-error" />
              </div>
              <p className="text-sm text-surface-300 font-medium">Failed to load graph</p>
              <p className="text-xs text-surface-500 mt-1 mb-4">
                Check your connection and try again
              </p>
              <button onClick={loadData} className="btn-primary text-sm">
                Retry
              </button>
            </div>
          )}

          {/* Empty state */}
          {!loading && !hasError && filteredData && filteredData.nodes.length === 0 && (
            <div className="absolute inset-0 z-10 flex flex-col items-center justify-center">
              <div className="flex h-12 w-12 items-center justify-center rounded-full bg-surface-800 mb-3">
                <Info size={22} className="text-surface-500" />
              </div>
              <p className="text-sm text-surface-300 font-medium">
                {isFiltered ? "No matching entities" : "No graph entities found"}
              </p>
              <p className="text-xs text-surface-500 mt-1">
                {isFiltered
                  ? "Try a different filter term"
                  : "Ingest some data to populate the knowledge graph"}
              </p>
              {isFiltered && (
                <button
                  onClick={() => setFilterText("")}
                  className="btn-ghost text-xs text-accent-300 mt-3"
                >
                  Clear filter
                </button>
              )}
            </div>
          )}

          {/* D3 renders into the SVG inside this container */}
        </div>
      </div>

      {/* ── Selected node info panel ────────────────────────────────────── */}
      {selectedNode && (
        <div className="card-base p-4 animate-fade-in">
          <div className="flex items-start justify-between gap-4">
            <div className="flex items-start gap-3 min-w-0">
              <span
                className="mt-0.5 block h-3 w-3 shrink-0 rounded-full"
                style={{ backgroundColor: getColor(selectedNode.type) }}
              />
              <div className="min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <h3 className="text-sm font-semibold text-white truncate">
                    {selectedNode.name}
                  </h3>
                  <span
                    className="text-[10px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded"
                    style={{
                      backgroundColor: `${getColor(selectedNode.type)}20`,
                      color: getColor(selectedNode.type),
                    }}
                  >
                    {selectedNode.type}
                  </span>
                </div>
                {selectedNode.summary && (
                  <p className="text-sm text-surface-400 mt-1.5 leading-relaxed max-w-2xl">
                    {selectedNode.summary}
                  </p>
                )}
                <div className="flex items-center gap-3 mt-2 text-[11px] text-surface-500">
                  <span className="font-mono">ID: {selectedNode.id}</span>
                  <span>Created {timeAgo(selectedNode.created_at)}</span>
                </div>
              </div>
            </div>
            <button
              onClick={() => setSelectedNode(null)}
              className="btn-ghost p-1 rounded-md text-surface-400 hover:text-white shrink-0"
            >
              <X size={16} />
            </button>
          </div>
        </div>
      )}

      {/* ── Legend ──────────────────────────────────────────────────────── */}
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
    </div>
  );
}
