# User Graph Explorer — Implementation Guide

> **Domain:** Admin Dashboard
> **SRS Phase:** Phase 4 — Dashboard & SDKs (Week 10-12)
> **Requirements:** DASH-04, KG-09, KG-10, KG-11
> **Doc Dependencies:** [01-nextjs-setup.md](01-nextjs-setup.md), [02-tenant-management.md](02-tenant-management.md), [04-knowledge-graph/02-entity-operations.md](../04-knowledge-graph/02-entity-operations.md)

---

## 1. Overview

The graph explorer provides an interactive visualisation of a user's entity knowledge graph. It renders entity nodes and their relationships as an interactive, zoomable, draggable graph that administrators can explore to understand what the system knows about a user.

### 1.1 Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Cytoscape.js** | Mature graph visualisation library with performance handling for large graphs. D3-force is more flexible but requires more code for the same result. Cytoscape has built-in layouts, styling, and event handling. |
| **Depth-limited neighbourhood** | For large graphs (>500 nodes), automatically limit to depth-2 neighbourhood from the user node. Prevents browser freeze. |
| **Click-to-inspect panel** | Clicking a node opens a side panel with entity details, facts, and related sessions. Avoids clutter on the canvas. |
| **Data from two API calls** | Nodes first, then edges. The backend separates these endpoints for pagination and filtering. The explorer combines them client-side. |

---

## 2. Page Structure

### 2.1 Route and Layout

```
/dashboard/orgs/[orgId]/users/[userId]/graph
   ↑ org selector           ↑ user selector        ↑ graph explorer
```

The graph explorer is nested under org → user to provide context. The breadcrumb:
```
Organisations > Acme Corp > Users > user_123 > Graph
```

### 2.2 Page Component

```typescript
// src/app/dashboard/orgs/[orgId]/users/[userId]/graph/page.tsx
"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useParams } from "next/navigation";
import { GraphCanvas } from "@/components/graph/graph-canvas";
import { EntityDetailPanel } from "@/components/graph/entity-detail-panel";
import { GraphSearch } from "@/components/graph/graph-search";
import { Button } from "@/components/ui/button";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { AlertTriangle, ZoomIn, ZoomOut, RefreshCw } from "lucide-react";

interface GraphNode {
  id: string;
  name: string;
  type: string;
  summary?: string;
  created_at: string;
}

interface GraphEdge {
  id: string;
  source: string;
  target: string;
  predicate: string;
  fact?: string;
}

export default function GraphExplorerPage() {
  const params = useParams();
  const orgId = params.orgId as string;
  const userId = params.userId as string;

  const [nodes, setNodes] = useState<GraphNode[]>([]);
  const [edges, setEdges] = useState<GraphEdge[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [showLargeGraphWarning, setShowLargeGraphWarning] = useState(false);

  const canvasRef = useRef<{ zoomIn: () => void; zoomOut: () => void; centerOn: (id: string) => void }>(null);

  const fetchGraph = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      // Fetch nodes and edges in parallel
      const [nodesRes, edgesRes] = await Promise.all([
        fetch(
          `/api/proxy/users/${userId}/graph/nodes?limit=200`
        ),
        fetch(
          `/api/proxy/users/${userId}/graph/edges`
        ),
      ]);

      if (!nodesRes.ok || !edgesRes.ok) {
        throw new Error("Failed to fetch graph data");
      }

      const nodesData: { data: GraphNode[] } = await nodesRes.json();
      const edgesData: { data: GraphEdge[] } = await edgesRes.json();

      const nodeList = nodesData.data || [];
      const edgeList = edgesData.data || [];

      // Show warning for large graphs
      if (nodeList.length > 500) {
        setShowLargeGraphWarning(true);
        // Limit to depth-2 neighbourhood from user node
        const userNode = nodeList.find(
          (n) => n.id === userId || n.name === userId
        );
        if (userNode) {
          const depth2Ids = getDepth2Subgraph(userNode.id, nodeList, edgeList);
          setNodes(nodeList.filter((n) => depth2Ids.has(n.id)));
          setEdges(
            edgeList.filter(
              (e) => depth2Ids.has(e.source) && depth2Ids.has(e.target)
            )
          );
        } else {
          setNodes(nodeList.slice(0, 200));
          setEdges(edgeList.slice(0, 500));
        }
      } else {
        setNodes(nodeList);
        setEdges(edgeList);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load graph");
    } finally {
      setLoading(false);
    }
  }, [userId]);

  useEffect(() => {
    fetchGraph();
  }, [fetchGraph]);

  function handleNodeClick(node: GraphNode) {
    setSelectedNode(node);
  }

  function handleSearchResult(nodeId: string) {
    canvasRef.current?.centerOn(nodeId);
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-[calc(100vh-200px)]">
        <div className="text-center">
          <div className="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full mx-auto mb-4" />
          <p className="text-muted-foreground">Loading graph...</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-center h-[calc(100vh-200px)]">
        <Alert variant="destructive" className="max-w-md">
          <AlertTriangle className="h-4 w-4" />
          <AlertTitle>Error Loading Graph</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      </div>
    );
  }

  return (
    <div className="h-[calc(100vh-200px)] flex flex-col">
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <div>
          <h1 className="text-2xl font-bold">Graph Explorer</h1>
          <p className="text-sm text-muted-foreground">
            User: {userId} · {nodes.length} nodes, {edges.length} edges
          </p>
        </div>
        <div className="flex items-center gap-2">
          <GraphSearch
            nodes={nodes}
            onSelect={handleSearchResult}
          />
          <Button variant="outline" size="sm" onClick={() => canvasRef.current?.zoomIn()}>
            <ZoomIn className="h-4 w-4" />
          </Button>
          <Button variant="outline" size="sm" onClick={() => canvasRef.current?.zoomOut()}>
            <ZoomOut className="h-4 w-4" />
          </Button>
          <Button variant="outline" size="sm" onClick={fetchGraph}>
            <RefreshCw className="h-4 w-4" />
          </Button>
        </div>
      </div>

      {/* Large graph warning */}
      {showLargeGraphWarning && (
        <Alert className="mb-4">
          <AlertTriangle className="h-4 w-4" />
          <AlertTitle>Large Graph</AlertTitle>
          <AlertDescription>
            This graph has over 500 nodes. Showing depth-2 neighbourhood.
            <Button
              variant="link"
              className="px-1 h-auto"
              onClick={() => {
                setShowLargeGraphWarning(false);
                // Show full graph (may be slow)
                fetchGraph();
              }}
            >
              Show full graph
            </Button>
          </AlertDescription>
        </Alert>
      )}

      {/* Graph + Detail Panel */}
      <div className="flex-1 flex gap-4 min-h-0">
        <div className="flex-1 border rounded-lg overflow-hidden bg-card">
          <GraphCanvas
            ref={canvasRef}
            nodes={nodes}
            edges={edges}
            onNodeClick={handleNodeClick}
            selectedNodeId={selectedNode?.id}
          />
        </div>

        {selectedNode && (
          <div className="w-80 border rounded-lg overflow-y-auto">
            <EntityDetailPanel
              node={selectedNode}
              orgId={orgId}
              userId={userId}
              onClose={() => setSelectedNode(null)}
            />
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * Get all node IDs within 2 hops of the given root node.
 * Used to limit large graphs to a manageable neighbourhood.
 */
function getDepth2Subgraph(
  rootId: string,
  nodes: GraphNode[],
  edges: GraphEdge[]
): Set<string> {
  const nodeIds = new Set<string>([rootId]);
  const adjacency = new Map<string, string[]>();

  // Build adjacency list
  for (const edge of edges) {
    if (!adjacency.has(edge.source)) adjacency.set(edge.source, []);
    if (!adjacency.has(edge.target)) adjacency.set(edge.target, []);
    adjacency.get(edge.source)!.push(edge.target);
    adjacency.get(edge.target)!.push(edge.source);
  }

  // BFS depth 1
  const depth1 = adjacency.get(rootId) || [];
  for (const n of depth1) nodeIds.add(n);

  // BFS depth 2
  for (const n1 of depth1) {
    const depth2 = adjacency.get(n1) || [];
    for (const n2 of depth2) nodeIds.add(n2);
  }

  return nodeIds;
}
```

---

## 3. Graph Canvas Component (Cytoscape.js)

### 3.1 Component

```typescript
// src/components/graph/graph-canvas.tsx
"use client";

import { useEffect, useRef, useImperativeHandle, forwardRef } from "react";
import cytoscape, { Core, EventObject, NodeSingular } from "cytoscape";

interface GraphNode {
  id: string;
  name: string;
  type: string;
}

interface GraphEdge {
  id: string;
  source: string;
  target: string;
  predicate: string;
}

interface GraphCanvasProps {
  nodes: GraphNode[];
  edges: GraphEdge[];
  onNodeClick: (node: GraphNode) => void;
  selectedNodeId?: string;
}

export interface GraphCanvasHandle {
  zoomIn: () => void;
  zoomOut: () => void;
  centerOn: (nodeId: string) => void;
}

// Entity type → visual style mapping
const NODE_STYLES: Record<string, { color: string; shape: cytoscape.Css.NodeShape }> = {
  Person: { color: "#3B82F6", shape: "ellipse" },       // Blue circle
  Company: { color: "#10B981", shape: "diamond" },       // Green diamond
  Product: { color: "#F59E0B", shape: "square" },        // Orange square
  Location: { color: "#8B5CF6", shape: "round-rectangle" },  // Purple rounded rect
  Organisation: { color: "#10B981", shape: "diamond" },      // Green diamond (same as Company)
  Event: { color: "#EF4444", shape: "triangle" },            // Red triangle
  Date: { color: "#6B7280", shape: "round-rectangle" },      // Grey rounded rect
  default: { color: "#6B7280", shape: "ellipse" },           // Grey ellipse for unknown types
};

export const GraphCanvas = forwardRef<GraphCanvasHandle, GraphCanvasProps>(
  function GraphCanvas({ nodes, edges, onNodeClick, selectedNodeId }, ref) {
    const containerRef = useRef<HTMLDivElement>(null);
    const cyRef = useRef<Core | null>(null);

    // Expose zoom/center methods to parent
    useImperativeHandle(ref, () => ({
      zoomIn() {
        cyRef.current?.zoom(cyRef.current.zoom() * 1.3);
      },
      zoomOut() {
        cyRef.current?.zoom(cyRef.current.zoom() * 0.7);
      },
      centerOn(nodeId: string) {
        const node = cyRef.current?.getElementById(nodeId);
        if (node) {
          cyRef.current?.animate({
            center: { eles: node },
            zoom: 2,
            duration: 300,
          });
        }
      },
    }));

    useEffect(() => {
      if (!containerRef.current) return;

      // Map entity type to Cytoscape style
      const getNodeStyle = (type: string) =>
        NODE_STYLES[type] || NODE_STYLES.default;

      // Initialise Cytoscape
      const cy = cytoscape({
        container: containerRef.current,
        elements: [
          // Nodes
          ...nodes.map((n) => ({
            data: {
              id: n.id,
              label: n.name,
              type: n.type,
            },
          })),
          // Edges
          ...edges.map((e) => ({
            data: {
              id: e.id,
              source: e.source,
              target: e.target,
              label: e.predicate,
            },
          })),
        ],
        style: [
          // Node style — varies by entity type
          ...Object.entries(NODE_STYLES).map(([type, style]) => ({
            selector: `node[type = "${type}"]`,
            style: {
              "background-color": style.color,
              shape: style.shape,
              width: 40,
              height: 40,
              "border-color": "#fff",
              "border-width": 2,
              label: "data(label)",
              "font-size": "11px",
              "text-valign": "bottom",
              "text-halign": "center",
              "padding-top": "10px",
              color: "#374151",
              "text-wrap": "ellipsis",
              "text-max-width": "80px",
            },
          })),
          // Default node style (for unmapped types)
          {
            selector: "node",
            style: {
              "background-color": NODE_STYLES.default.color,
              shape: NODE_STYLES.default.shape,
              width: 40,
              height: 40,
              "border-color": "#fff",
              "border-width": 2,
              label: "data(label)",
              "font-size": "11px",
              "text-valign": "bottom",
              "text-halign": "center",
              "padding-top": "10px",
              color: "#374151",
            },
          },
          // Edge style
          {
            selector: "edge",
            style: {
              width: 1.5,
              "line-color": "#D1D5DB",
              "target-arrow-color": "#D1D5DB",
              "target-arrow-shape": "triangle",
              "curve-style": "bezier",
              label: "data(label)",
              "font-size": "9px",
              color: "#9CA3AF",
              "text-rotation": "autorotate",
            },
          },
          // Selected node highlight
          {
            selector: "node:selected",
            style: {
              "border-color": "#3B82F6",
              "border-width": 4,
              "shadow-blur": 10,
              "shadow-color": "#3B82F6",
              "shadow-opacity": 0.4,
            },
          },
        ],
        layout: {
          name: "cose",
          animate: true,
          animationDuration: 500,
          nodeRepulsion: () => 8000,
          idealEdgeLength: () => 150,
          gravity: 0.25,
          numIter: 1000,
        },
        userZoomingEnabled: true,
        userPanningEnabled: true,
        boxSelectionEnabled: false,
      });

      // Click handler
      cy.on("tap", "node", (event: EventObject) => {
        const node = event.target as NodeSingular;
        const nodeData = node.data();
        onNodeClick({
          id: nodeData.id,
          name: nodeData.label,
          type: nodeData.type,
        });
      });

      // Background click — deselect
      cy.on("tap", (event: EventObject) => {
        if (event.target === cy) {
          // Clicked on background
        }
      });

      cyRef.current = cy;

      // Fit to viewport on initial render
      cy.fit(undefined, 50);

      return () => {
        cy.destroy();
        cyRef.current = null;
      };
    }, [nodes, edges]); // Re-initialise when data changes

    // Highlight selected node
    useEffect(() => {
      if (cyRef.current && selectedNodeId) {
        cyRef.current.getElementById(selectedNodeId)?.select();
      }
    }, [selectedNodeId]);

    return (
      <div
        ref={containerRef}
        className="w-full h-full"
        style={{ minHeight: "400px" }}
      />
    );
  }
);
```

---

## 4. Entity Detail Panel

### 4.1 Component

```typescript
// src/components/graph/entity-detail-panel.tsx
"use client";

import { useState, useEffect } from "react";
import { X, FileText, Link2, Clock } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";

interface GraphNode {
  id: string;
  name: string;
  type: string;
  summary?: string;
  created_at: string;
}

interface EntityDetail {
  summary?: string;
  facts: Array<{
    id: string;
    content: string;
    confidence: number;
    valid_from: string;
  }>;
  related_sessions: Array<{
    id: string;
    name: string;
    message_count: number;
  }>;
}

interface EntityDetailPanelProps {
  node: GraphNode;
  orgId: string;
  userId: string;
  onClose: () => void;
}

const TYPE_COLORS: Record<string, string> = {
  Person: "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200",
  Company: "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200",
  Product: "bg-orange-100 text-orange-800 dark:bg-orange-900 dark:text-orange-200",
  Location: "bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200",
  default: "bg-gray-100 text-gray-800 dark:bg-gray-900 dark:text-gray-200",
};

export function EntityDetailPanel({ node, orgId, userId, onClose }: EntityDetailPanelProps) {
  const [detail, setDetail] = useState<EntityDetail | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchDetail();
  }, [node.id]);

  async function fetchDetail() {
    setLoading(true);
    try {
      // Fetch node details including edges and summary
      const nodeRes = await fetch(
        `/api/proxy/users/${userId}/graph/nodes/${node.id}`
      );
      const factsRes = await fetch(
        `/api/proxy/users/${userId}/facts?entity=${node.name}&limit=20`
      );

      const [nodeData, factsData] = await Promise.all([
        nodeRes.json(),
        factsRes.json(),
      ]);

      setDetail({
        summary: nodeData.summary,
        facts: (factsData.data || []).slice(0, 20),
        related_sessions: nodeData.sessions || [],
      });
    } finally {
      setLoading(false);
    }
  }

  const colorClass = TYPE_COLORS[node.type] || TYPE_COLORS.default;

  return (
    <div className="p-4">
      {/* Header */}
      <div className="flex items-start justify-between mb-4">
        <div>
          <h3 className="font-semibold text-lg">{node.name}</h3>
          <Badge variant="secondary" className={colorClass}>
            {node.type}
          </Badge>
        </div>
        <Button variant="ghost" size="sm" onClick={onClose}>
          <X className="h-4 w-4" />
        </Button>
      </div>

      <Separator className="my-4" />

      {loading ? (
        <div className="space-y-3">
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-3/4" />
          <Skeleton className="h-20 w-full" />
        </div>
      ) : detail ? (
        <div className="space-y-4">
          {/* Summary */}
          {detail.summary && (
            <div>
              <h4 className="text-sm font-medium flex items-center gap-2 mb-1">
                <FileText className="h-3.5 w-3.5 text-muted-foreground" />
                Summary
              </h4>
              <p className="text-sm text-muted-foreground">{detail.summary}</p>
            </div>
          )}

          {/* Facts */}
          <div>
            <h4 className="text-sm font-medium flex items-center gap-2 mb-1">
              <Link2 className="h-3.5 w-3.5 text-muted-foreground" />
              Facts ({detail.facts.length})
            </h4>
            {detail.facts.length === 0 ? (
              <p className="text-sm text-muted-foreground">No facts</p>
            ) : (
              <div className="space-y-2 max-h-60 overflow-y-auto">
                {detail.facts.map((fact) => (
                  <div
                    key={fact.id}
                    className="text-sm p-2 bg-muted rounded-md"
                  >
                    <p>{fact.content}</p>
                    <div className="flex items-center gap-2 mt-1 text-xs text-muted-foreground">
                      <span>confidence: {(fact.confidence * 100).toFixed(0)}%</span>
                      {fact.valid_from && (
                        <span>from: {new Date(fact.valid_from).toLocaleDateString()}</span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Related Sessions */}
          <div>
            <h4 className="text-sm font-medium flex items-center gap-2 mb-1">
              <Clock className="h-3.5 w-3.5 text-muted-foreground" />
              Related Sessions ({detail.related_sessions.length})
            </h4>
            {detail.related_sessions.length === 0 ? (
              <p className="text-sm text-muted-foreground">No related sessions</p>
            ) : (
              <div className="space-y-1">
                {detail.related_sessions.map((s) => (
                  <div
                    key={s.id}
                    className="text-sm p-1.5 hover:bg-muted rounded cursor-pointer"
                  >
                    <p className="font-medium">{s.name || s.id}</p>
                    <p className="text-xs text-muted-foreground">
                      {s.message_count} messages
                    </p>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">Failed to load details</p>
      )}
    </div>
  );
}
```

---

## 5. Graph Search Component

```typescript
// src/components/graph/graph-search.tsx
"use client";

import { useState, useMemo } from "react";
import { Search } from "lucide-react";
import { Input } from "@/components/ui/input";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
} from "@/components/ui/command";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";

interface GraphNode {
  id: string;
  name: string;
  type: string;
}

interface GraphSearchProps {
  nodes: GraphNode[];
  onSelect: (nodeId: string) => void;
}

export function GraphSearch({ nodes, onSelect }: GraphSearchProps) {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");

  const filtered = useMemo(
    () =>
      nodes.filter((n) =>
        n.name.toLowerCase().includes(value.toLowerCase()) ||
        n.type.toLowerCase().includes(value.toLowerCase())
      ),
    [nodes, value]
  );

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="Search entities..."
            className="pl-9 w-64"
            value={value}
            onChange={(e) => {
              setValue(e.target.value);
              setOpen(true);
            }}
            onFocus={() => setOpen(true)}
          />
        </div>
      </PopoverTrigger>
      <PopoverContent className="w-64 p-0" align="start">
        <Command>
          <CommandInput
            placeholder="Search entities..."
            value={value}
            onValueChange={setValue}
          />
          <CommandEmpty>No entities found</CommandEmpty>
          <CommandGroup className="max-h-48 overflow-y-auto">
            {filtered.slice(0, 20).map((node) => (
              <CommandItem
                key={node.id}
                onSelect={() => {
                  setValue(node.name);
                  setOpen(false);
                  onSelect(node.id);
                }}
              >
                <div className="flex items-center gap-2">
                  <span className="text-xs text-muted-foreground">[{node.type}]</span>
                  <span>{node.name}</span>
                </div>
              </CommandItem>
            ))}
          </CommandGroup>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
```

---

## 6. Performance Considerations

### 6.1 Graph Size Limits

| Node Count | Behaviour |
|------------|-----------|
| 0-100 | Full graph, cose layout, smooth interaction |
| 100-500 | Full graph, cose layout, may be slightly slower |
| 500-2000 | **Warning shown**, default to depth-2 neighbourhood |
| 2000+ | Depth-2 neighbourhood only, user can opt in to full view |

### 6.2 Cytoscape Performance Options

```typescript
// Performance optimisations for large graphs
const cy = cytoscape({
  // ... other options
  minZoom: 0.1,
  maxZoom: 4,
  // Performance flags
  motionBlur: true,         // Smoother animation for large graphs
  pixelRatio: 1,            // Reduce canvas resolution for large graphs
  hideEdgesOnViewport: true, // Don't render edges during zoom/pan
  textureOnViewport: true,   // Use texture rendering during viewport changes
});
```

### 6.3 Lazy Loading Approach

For users with very large graphs, implement neighbourhood expansion on click:

```typescript
// When a user clicks a collapsed node (not in the current viewport),
// fetch its depth-1 neighbourhood and add it to the graph

async function expandNode(nodeId: string) {
  const res = await fetch(`/api/proxy/users/${userId}/graph/nodes/${nodeId}/neighbourhood?depth=1`);
  const { nodes: newNodes, edges: newEdges } = await res.json();

  // Add to Cytoscape
  const cy = cyRef.current;
  if (!cy) return;

  for (const n of newNodes) {
    if (!cy.getElementById(n.id).length) {
      cy.add({ data: { id: n.id, label: n.name, type: n.type } });
    }
  }
  for (const e of newEdges) {
    if (!cy.getElementById(e.id).length) {
      cy.add({
        data: {
          id: e.id,
          source: e.source,
          target: e.target,
          label: e.predicate,
        },
      });
    }
  }

  // Re-run layout on new elements
  cy.layout({ name: "cose", animate: true, fit: false });
}
```

---

## 7. Entity Type Styling Reference

| Entity Type | Colour | Shape | CSS Class |
|-------------|--------|-------|-----------|
| `Person` | Blue `#3B82F6` | Ellipse | `bg-blue-100` |
| `Company` | Green `#10B981` | Diamond | `bg-green-100` |
| `Organisation` | Green `#10B981` | Diamond | `bg-green-100` |
| `Product` | Orange `#F59E0B` | Square (rectangle) | `bg-orange-100` |
| `Location` | Purple `#8B5CF6` | Round rectangle | `bg-purple-100` |
| `Event` | Red `#EF4444` | Triangle | `bg-red-100` |
| `Date` | Grey `#6B7280` | Round rectangle | `bg-gray-100` |
| `Role` | Teal `#14B8A6` | Ellipse | `bg-teal-100` |
| *(unknown)* | Grey `#6B7280` | Ellipse | `bg-gray-100` |

---

## 8. API Endpoints Consumed

| Endpoint | Method | Usage |
|----------|--------|-------|
| `/api/proxy/users/{userId}/graph/nodes` | GET | Fetch all entity nodes (paginated) |
| `/api/proxy/users/{userId}/graph/nodes/{nodeId}` | GET | Fetch single node with edges and metadata |
| `/api/proxy/users/{userId}/graph/nodes/{nodeId}/neighbourhood` | GET | Fetch depth-1 neighbourhood (for expansion) |
| `/api/proxy/users/{userId}/graph/edges` | GET | Fetch all edges (supports filtering) |
| `/api/proxy/users/{userId}/facts` | GET | Fetch facts filtered by entity name |

---

## 9. Testing

```typescript
// __tests__/components/graph/graph-canvas.test.tsx
import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { GraphCanvas } from "@/components/graph/graph-canvas";

describe("GraphCanvas", () => {
  const sampleNodes = [
    { id: "1", name: "Alice", type: "Person" },
    { id: "2", name: "Acme Corp", type: "Company" },
  ];

  const sampleEdges = [
    { id: "e1", source: "1", target: "2", predicate: "works_at" },
  ];

  it("renders without crashing", () => {
    const { container } = render(
      <GraphCanvas
        nodes={sampleNodes}
        edges={sampleEdges}
        onNodeClick={() => {}}
      />
    );
    expect(container.querySelector("div")).toBeTruthy();
  });

  it("renders a canvas element", () => {
    const { container } = render(
      <GraphCanvas
        nodes={sampleNodes}
        edges={sampleEdges}
        onNodeClick={() => {}}
      />
    );
    // Cytoscape renders inside the container div
    expect(container.innerHTML).toContain("canvas");
  });
});
```

---

## 10. Open Questions

| # | Question | Decision |
|---|----------|----------|
| Q1 | Should we support exporting the graph as an image (PNG/SVG)? | Yes — add an "Export as PNG" button in the toolbar. Cytoscape has `cy.png()` built-in. |
| Q2 | Should the graph support filtering by entity type (show only Person nodes)? | Yes — add a filter dropdown in the toolbar. Filtering is client-side (hide/show elements). |
| Q3 | Should we store graph layout positions? | No — layouts are computed client-side each time. Positions are ephemeral. |
| Q4 | Should we support 3D graph visualisation? | No — 2D is sufficient and performs significantly better. |

---

*Corresponding SRS requirements: DASH-04, KG-09, KG-10, KG-11. Next: [04-analytics-panels.md](04-analytics-panels.md) for usage analytics.*
