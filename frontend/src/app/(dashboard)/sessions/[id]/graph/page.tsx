"use client";

import { useEffect, useRef, useState } from "react";
import { useParams, useSearchParams } from "next/navigation";
import * as d3 from "d3";
import { AlertCircle } from "lucide-react";
import SessionTabs from "../tabs";

const TYPE_COLORS: Record<string, string> = {
  person: "#14488C", organization: "#1453A6", location: "#8FAFD9",
  event: "#1747A6", concept: "#6A8DB8",
};
function getColor(type: string): string {
  return TYPE_COLORS[type?.toLowerCase()] ?? "#4A6D96";
}

export default function SessionGraphPage() {
  const params = useParams();
  const searchParams = useSearchParams();
  const sessionId = params.id as string;
  const userId = searchParams.get("userId") ?? "";
  const svgRef = useRef<SVGSVGElement>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!userId || !svgRef.current) return;
    let simulation: d3.Simulation<any, any> | null = null;

    async function loadGraph() {
      setLoading(true);
      setError("");
      try {
        const token = sessionStorage.getItem("mg_access_token");
        const headers: Record<string, string> = {};
        if (token) headers["Authorization"] = `Bearer ${token}`;

        const nodeRes = await fetch(`http://localhost:8000/v1/users/${userId}/graph/nodes?limit=100&session_id=${sessionId}`, { headers });
        if (!nodeRes.ok) throw new Error("Failed to load graph data");
        const nodeData = await nodeRes.json();
        const items: any[] = (nodeData.data as any)?.items ?? [];

        const nodes = items.map((n: any) => ({ id: n.id, name: n.name, type: n.type }));
        const links: any[] = [];
        const seen = new Set<string>();

        for (let i = 0; i < items.length; i += 5) {
          const batch = items.slice(i, i + 5);
          const results = await Promise.allSettled(
            batch.map((n: any) =>
              fetch(`http://localhost:8000/v1/users/${userId}/graph/edges?subject_id=${n.id}&limit=50`, { headers })
                .then(r => r.json())
                .catch(() => ({ data: { items: [] } })),
            ),
          );
          for (const r of results) {
            if (r.status === "fulfilled") {
              for (const e of (r.value.data as any)?.items ?? []) {
                const key = [e.source_id, e.target_id].sort().join("|");
                if (!seen.has(key)) { seen.add(key); links.push({ source: e.source_id, target: e.target_id, type: e.type }); }
              }
            }
          }
        }

        // Render D3 graph
        const svg = d3.select(svgRef.current!);
        svg.selectAll("*").remove();
        const width = svgRef.current!.clientWidth || 600;
        const height = 500;

        simulation = d3.forceSimulation(nodes as any)
          .force("link", d3.forceLink(links).id((d: any) => d.id).distance(100))
          .force("charge", d3.forceManyBody().strength(-200))
          .force("center", d3.forceCenter(width / 2, height / 2))
          .force("collide", d3.forceCollide(30));

        const g = svg.append("g");
        const zoom = d3.zoom().scaleExtent([0.15, 5]).on("zoom", (event) => g.attr("transform", event.transform));
        svg.call(zoom as any);

        const link = g.append("g").selectAll("line").data(links).join("line")
          .attr("stroke", "rgba(143,175,217,0.2)").attr("stroke-width", 1);

        const node = g.append("g").selectAll("g").data(nodes).join("g")
          .call(d3.drag<any, any>()
            .on("start", (event, d) => { if (!event.active) simulation?.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
            .on("drag", (event, d) => { d.fx = event.x; d.fy = event.y; })
            .on("end", (event, d) => { if (!event.active) simulation?.alphaTarget(0); d.fx = null; d.fy = null; }) as any);

        node.append("circle").attr("r", 6).attr("fill", (d: any) => getColor(d.type));
        node.append("text").text((d: any) => d.name).attr("dx", 10).attr("dy", 3)
          .attr("font-size", "10px").attr("fill", "rgba(242,242,242,0.7)").attr("font-family", "var(--font-sans)");

        simulation.on("tick", () => {
          link.attr("x1", (d: any) => d.source.x).attr("y1", (d: any) => d.source.y)
              .attr("x2", (d: any) => d.target.x).attr("y2", (d: any) => d.target.y);
          node.attr("transform", (d: any) => `translate(${d.x},${d.y})`);
        });

        // Zoom to fit
        setTimeout(() => {
          const bounds = (svg.node() as any)?.getBBox();
          if (bounds) {
            const scale = Math.min(width / bounds.width, height / bounds.height, 1.5) * 0.9;
            const tx = width / 2 - bounds.x * scale - bounds.width * scale / 2;
            const ty = height / 2 - bounds.y * scale - bounds.height * scale / 2;
            svg.transition().duration(500).call(zoom.transform as any, d3.zoomIdentity.translate(tx, ty).scale(scale));
          }
        }, 100);

        setLoading(false);
      } catch (err: any) {
        setError(err.message);
        setLoading(false);
      }
    }
    loadGraph();
    return () => { simulation?.stop(); };
  }, [userId, sessionId]);

  return (
    <div>
      <SessionTabs sessionId={sessionId} userId={userId} activeTab="graph" />
      {loading ? (
        <div className="card-base p-6 flex items-center justify-center h-[400px]">
          <div className="animate-spin h-8 w-8 border-2 border-brand-500 border-t-transparent rounded-full" />
        </div>
      ) : error ? (
        <div className="card-base p-6 flex items-center gap-3 text-error text-sm"><AlertCircle size={18} />{error}</div>
      ) : (
        <div className="card-base overflow-hidden">
          <svg ref={svgRef} width="100%" height="500" className="block" />
        </div>
      )}
    </div>
  );
}
