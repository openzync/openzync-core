# Analytics Panels — Implementation Guide

> **Domain:** Admin Dashboard
> **SRS Phase:** Phase 4 — Dashboard & SDKs (Week 10-12)
> **Requirements:** DASH-06, SRS §11.4 (Grafana dashboards)
> **Doc Dependencies:** [01-nextjs-setup.md](01-nextjs-setup.md), [12-observability/02-metrics-definitions.md](../12-observability/02-metrics-definitions.md)

---

## 1. Overview

The analytics dashboard provides platform administrators with real-time and historical usage metrics: API request rates, error rates, latency percentiles, active users, graph growth, token usage, and worker queue health.

### 1.1 Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Mimir as primary data source** | Metrics are already collected by the LGTM stack (Alloy → Mimir). Querying Mimir via PromQL gives us production-grade aggregated data without hitting the API DB. |
| **Grafana embedded panels as default** | If Grafana is available (it's part of the LGTM stack), embedded iframes are zero-code, more powerful, and already exist. Recharts is the fallback for standalone deployments. |
| **Recharts for standalone** | The dashboard may be deployed separately from the LGTM stack. In that case, the `api/proxy` route queries the backend's internal Prometheus endpoint or a lightweight aggregator. |
| **Time range selector** | All charts default to last 24h, with options for 1h, 6h, 24h, 7d, 30d. |

### 1.2 Architecture

**Option A: Grafana Embed (Recommended — LGTM available)**

```
Dashboard (Next.js)                 Grafana
      │                                │
      │  <iframe src="grafana.example.com/d/abc123/...?orgId=...&from=...&to=..." />
      │───────────────────────────────►│
      │                                │
      │◄─── Rendered panel ───────────│
```

**Option B: Recharts + API Proxy (Standalone)**

```
Dashboard (Next.js)            Next.js API Proxy              Backend / Mimir
      │                              │                              │
      │  GET /api/proxy/metrics/     │  GET http://mimir:9009/      │
      │  ?query=...&from=...&to=...  │  /prometheus/api/v1/query    │
      │─────────────────────────────►│─────────────────────────────►│
      │                              │◄─── Prometheus response ────│
      │◄─── Aggregated data ───────│                              │
      │                              │                              │
      │  Recharts renders panel      │                              │
```

---

## 2. Data Source Configuration

### 2.1 Prometheus/Mimir Query Proxy

```typescript
// src/app/api/proxy/metrics/route.ts
// Proxy PromQL queries to Mimir from the dashboard

import { NextRequest, NextResponse } from "next/server";

const MIMIR_URL = process.env.MIMIR_URL || "http://mimir:9009/prometheus";

export async function GET(request: NextRequest) {
  const query = request.nextUrl.searchParams.get("query");
  const start = request.nextUrl.searchParams.get("start");
  const end = request.nextUrl.searchParams.get("end");
  const step = request.nextUrl.searchParams.get("step") || "60";

  if (!query) {
    return NextResponse.json({ error: "query parameter required" }, { status: 400 });
  }

  const params = new URLSearchParams({ query });
  if (start) params.set("start", start);
  if (end) params.set("end", end);
  if (step) params.set("step", step);

  try {
    const response = await fetch(
      `${MIMIR_URL}/api/v1/query_range?${params}`,
      {
        headers: {
          // Mimir auth (if configured)
          ...(process.env.MIMIR_TOKEN
            ? { Authorization: `Bearer ${process.env.MIMIR_TOKEN}` }
            : {}),
        },
      }
    );

    if (!response.ok) {
      throw new Error(`Mimir returned ${response.status}`);
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (err) {
    return NextResponse.json(
      { error: "Failed to query metrics" },
      { status: 502 }
    );
  }
}
```

### 2.2 Fallback: Backend Aggregate Endpoint

If Mimir is not available, the backend exposes an aggregate endpoint that queries the API database directly:

```
GET /v1/admin/stats?from=2026-06-04T00:00:00Z&to=2026-06-05T00:00:00Z
```

Response:
```json
{
  "api_requests": {
    "total": 15420,
    "by_endpoint": {
      "POST /memory": 8920,
      "GET /context": 4500,
      "GET /search": 1200,
      "other": 800
    },
    "error_rate": 0.023,
    "p50_latency_ms": 45,
    "p95_latency_ms": 210,
    "p99_latency_ms": 480
  },
  "active_users": {
    "daily": 342,
    "weekly": 1240,
    "monthly": 3800
  },
  "graph_nodes": {
    "total": 128000,
    "by_org": [
      {"org_id": "org_1", "count": 45000},
      {"org_id": "org_2", "count": 32000}
    ]
  },
  "token_usage": {
    "total_tokens": 85200000,
    "by_model": {
      "gpt-4o": 32000000,
      "gpt-4o-mini": 45200000,
      "text-embedding-3-small": 8000000
    },
    "estimated_cost_usd": 42.50
  },
  "worker_queue": {
    "high": 12,
    "low": 145
  }
}
```

---

## 3. Panel Definitions

### 3.1 API Request Rate (RPS)

**Purpose:** Monitor traffic patterns and detect traffic spikes.

**PromQL:**
```promql
// Requests per second by endpoint group
sum by (endpoint_group) (
  rate(openzep_http_requests_total[5m])
)
```

**Panel:**
```typescript
// src/components/analytics/api-rps-chart.tsx
"use client";

import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from "recharts";

interface RpsDataPoint {
  timestamp: string;
  memory: number;
  context: number;
  search: number;
  graph: number;
  admin: number;
}

export function ApiRpsChart({ data }: { data: RpsDataPoint[] }) {
  return (
    <div className="w-full">
      <h3 className="text-sm font-medium mb-2">API Request Rate (RPS)</h3>
      <ResponsiveContainer width="100%" height={250}>
        <LineChart data={data}>
          <CartesianGrid strokeDasharray="3 3" stroke="#E5E7EB" />
          <XAxis
            dataKey="timestamp"
            tick={{ fontSize: 11 }}
            tickFormatter={(v) => new Date(v).toLocaleTimeString()}
          />
          <YAxis tick={{ fontSize: 11 }} />
          <Tooltip
            labelFormatter={(v) => new Date(v).toLocaleString()}
          />
          <Legend />
          <Line
            type="monotone"
            dataKey="memory"
            stroke="#3B82F6"
            name="Memory"
            dot={false}
          />
          <Line
            type="monotone"
            dataKey="context"
            stroke="#10B981"
            name="Context"
            dot={false}
          />
          <Line
            type="monotone"
            dataKey="search"
            stroke="#F59E0B"
            name="Search"
            dot={false}
          />
          <Line
            type="monotone"
            dataKey="graph"
            stroke="#8B5CF6"
            name="Graph"
            dot={false}
          />
          <Line
            type="monotone"
            dataKey="admin"
            stroke="#EF4444"
            name="Admin"
            dot={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
```

### 3.2 Error Rate (5xx vs 4xx)

**Purpose:** Detect backend issues and client misconfigurations.

**PromQL:**
```promql
// 5xx rate
sum by (endpoint) (
  rate(openzep_http_requests_total{status_group="5xx"}[5m])
)

// 4xx rate
sum by (endpoint) (
  rate(openzep_http_requests_total{status_group="4xx"}[5m])
)
```

**Panel:**
```typescript
export function ErrorRateChart({ data }: { data: any[] }) {
  return (
    <div className="w-full">
      <h3 className="text-sm font-medium mb-2">Error Rate (per minute)</h3>
      <ResponsiveContainer width="100%" height={200}>
        <LineChart data={data}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey="timestamp" tick={{ fontSize: 11 }} />
          <YAxis tick={{ fontSize: 11 }} />
          <Tooltip />
          <Legend />
          <Line
            type="monotone"
            dataKey="error_5xx"
            stroke="#EF4444"
            name="5xx Errors"
            dot={false}
            strokeWidth={2}
          />
          <Line
            type="monotone"
            dataKey="error_4xx"
            stroke="#F59E0B"
            name="4xx Errors"
            dot={false}
            strokeWidth={2}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
```

### 3.3 Context Latency (p50/p95/p99)

**Purpose:** Monitor context assembly performance against SRS targets (p50 ≤ 50ms, p95 ≤ 300ms).

**PromQL:**
```promql
// Latency percentiles for GET /context
histogram_quantile(0.50, sum by (le) (rate(openzep_context_assembly_duration_seconds_bucket[5m])))
histogram_quantile(0.95, sum by (le) (rate(openzep_context_assembly_duration_seconds_bucket[5m])))
histogram_quantile(0.99, sum by (le) (rate(openzep_context_assembly_duration_seconds_bucket[5m])))
```

**Panel:**
```typescript
export function LatencyChart({ data }: { data: any[] }) {
  return (
    <div className="w-full">
      <h3 className="text-sm font-medium mb-2">Context Latency (ms)</h3>
      <ResponsiveContainer width="100%" height={250}>
        <LineChart data={data}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey="timestamp" tick={{ fontSize: 11 }} />
          <YAxis
            tick={{ fontSize: 11 }}
            tickFormatter={(v) => `${(v * 1000).toFixed(0)}ms`}
          />
          <Tooltip
            formatter={(value: number) => `${(value * 1000).toFixed(0)}ms`}
          />
          <Legend />
          <Line
            type="monotone"
            dataKey="p50"
            stroke="#10B981"
            name="p50"
            dot={false}
          />
          <Line
            type="monotone"
            dataKey="p95"
            stroke="#F59E0B"
            name="p95"
            dot={false}
          />
          <Line
            type="monotone"
            dataKey="p99"
            stroke="#EF4444"
            name="p99"
            dot={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
```

### 3.4 Active Users (Daily, Weekly, Monthly)

**Purpose:** Track platform adoption and usage patterns.

**PromQL (via recording rules):**
```promql
// Daily active users (count of distinct user_ids seen in last 24h)
sum by (org_id) (
  count_over_time(openzep_http_requests_total{endpoint="POST /memory"}[24h])
)

// Or via a gauge metric updated by a cron job
openzep_active_users{daily="true", weekly="true", monthly="true"}
```

**Panel:**
```typescript
export function ActiveUsersChart({ stats }: { stats: { daily: number; weekly: number; monthly: number } }) {
  const data = [
    { name: "Daily", value: stats.daily },
    { name: "Weekly", value: stats.weekly },
    { name: "Monthly", value: stats.monthly },
  ];

  return (
    <div className="w-full">
      <h3 className="text-sm font-medium mb-2">Active Users</h3>
      <ResponsiveContainer width="100%" height={200}>
        <BarChart data={data}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey="name" />
          <YAxis />
          <Tooltip />
          <Bar dataKey="value" fill="#3B82F6" radius={[4, 4, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
```

### 3.5 Graph Nodes Growth

**Purpose:** Track knowledge graph size over time by organisation.

**PromQL:**
```promql
// Graph nodes by org over time
openzep_graph_nodes_total
```

**Panel:**
```typescript
export function GraphGrowthChart({ data }: { data: any[] }) {
  return (
    <div className="w-full">
      <h3 className="text-sm font-medium mb-2">Graph Nodes Growth</h3>
      <ResponsiveContainer width="100%" height={250}>
        <LineChart data={data}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey="timestamp" tick={{ fontSize: 11 }} />
          <YAxis tick={{ fontSize: 11 }} />
          <Tooltip />
          <Legend />
          {/* One line per org — colour assigned dynamically */}
          {/* This is a simplified version; in practice, use AreaChart for stacking */}
          <Line
            type="monotone"
            dataKey="total"
            stroke="#3B82F6"
            name="Total Nodes"
            dot={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
```

### 3.6 Token Usage

**Purpose:** Track LLM costs by organisation and model. Critical for cost management.

**PromQL:**
```promql
// Tokens consumed by model (cumulative)
sum by (model) (
  rate(openzep_llm_tokens_total[5m])
)

// Tokens consumed by org
sum by (org_id) (
  rate(openzep_llm_tokens_total[5m])
)
```

**Panel:**
```typescript
export function TokenUsageChart({ data }: { data: any[] }) {
  return (
    <div className="w-full">
      <h3 className="text-sm font-medium mb-2">Token Usage & Cost Projection</h3>
      <div className="grid grid-cols-2 gap-4">
        {/* Model breakdown */}
        <ResponsiveContainer width="100%" height={200}>
          <PieChart>
            <Pie
              data={data.model_breakdown}
              dataKey="tokens"
              nameKey="model"
              cx="50%"
              cy="50%"
              outerRadius={80}
              label={({ model, percent }) =>
                `${model} (${(percent * 100).toFixed(0)}%)`
              }
            >
              {data.model_breakdown.map((_: any, idx: number) => (
                <Cell
                  key={idx}
                  fill={["#3B82F6", "#10B981", "#F59E0B", "#EF4444"][idx % 4]}
                />
              ))}
            </Pie>
            <Tooltip
              formatter={(value: number) => value.toLocaleString()}
            />
          </PieChart>
        </ResponsiveContainer>

        {/* Cost summary card */}
        <div className="flex flex-col justify-center p-4 border rounded-lg">
          <p className="text-sm text-muted-foreground">Estimated Daily Cost</p>
          <p className="text-3xl font-bold">${data.estimated_cost.toFixed(2)}</p>
          <p className="text-xs text-muted-foreground mt-1">
            {(data.total_tokens / 1000000).toFixed(1)}M tokens today
          </p>
        </div>
      </div>
    </div>
  );
}
```

### 3.7 Worker Queue Depth

**Purpose:** Monitor ARQ worker health — high queue depth indicates a processing bottleneck.

**PromQL:**
```promql
// Queue depth by priority
openzep_worker_queue_depth{queue="high"}
openzep_worker_queue_depth{queue="low"}
```

**Panel:**
```typescript
export function WorkerQueueChart({ data }: { data: any[] }) {
  return (
    <div className="w-full">
      <h3 className="text-sm font-medium mb-2">Worker Queue Depth</h3>
      <ResponsiveContainer width="100%" height={200}>
        <AreaChart data={data}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey="timestamp" tick={{ fontSize: 11 }} />
          <YAxis tick={{ fontSize: 11 }} />
          <Tooltip />
          <Legend />
          <Area
            type="monotone"
            dataKey="high"
            stackId="1"
            stroke="#EF4444"
            fill="#FEE2E2"
            name="High Priority"
          />
          <Area
            type="monotone"
            dataKey="low"
            stackId="1"
            stroke="#F59E0B"
            fill="#FEF3C7"
            name="Low Priority"
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
```

---

## 4. Analytics Dashboard Page

### 4.1 Page Component

```typescript
// src/app/dashboard/analytics/page.tsx
"use client";

import { useState, useEffect } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ApiRpsChart } from "@/components/analytics/api-rps-chart";
import { ErrorRateChart } from "@/components/analytics/error-rate-chart";
import { LatencyChart } from "@/components/analytics/latency-chart";
import { ActiveUsersChart } from "@/components/analytics/active-users-chart";
import { GraphGrowthChart } from "@/components/analytics/graph-growth-chart";
import { TokenUsageChart } from "@/components/analytics/token-usage-chart";
import { WorkerQueueChart } from "@/components/analytics/worker-queue-chart";

const TIME_RANGES = [
  { label: "Last Hour", value: "1h" },
  { label: "Last 6 Hours", value: "6h" },
  { label: "Last 24 Hours", value: "24h" },
  { label: "Last 7 Days", value: "7d" },
  { label: "Last 30 Days", value: "30d" },
];

export default function AnalyticsPage() {
  const [timeRange, setTimeRange] = useState("24h");
  const [metricsData, setMetricsData] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [dataSource, setDataSource] = useState<"grafana" | "recharts">("recharts");

  useEffect(() => {
    fetchMetrics();
  }, [timeRange]);

  async function fetchMetrics() {
    setLoading(true);
    try {
      const [start, end] = getTimeRange(timeRange);

      // Try Mimir first, fall back to backend stats endpoint
      try {
        const res = await fetch(
          `/api/proxy/metrics/aggregated?start=${start.toISOString()}&end=${end.toISOString()}`
        );
        if (res.ok) {
          setMetricsData(await res.json());
          return;
        }
      } catch {
        // Fallback to backend stats
      }

      // Fallback
      const res = await fetch(
        `/api/proxy/admin/stats?from=${start.toISOString()}&to=${end.toISOString()}`
      );
      if (res.ok) {
        setMetricsData(await res.json());
      }
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Analytics</h1>
          <p className="text-muted-foreground">
            Platform usage metrics and performance
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Select value={dataSource} onValueChange={setDataSource}>
            <SelectTrigger className="w-40">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="grafana">Grafana (Embed)</SelectItem>
              <SelectItem value="recharts">Built-in Charts</SelectItem>
            </SelectContent>
          </Select>
          <Select value={timeRange} onValueChange={setTimeRange}>
            <SelectTrigger className="w-40">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {TIME_RANGES.map((r) => (
                <SelectItem key={r.value} value={r.value}>
                  {r.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      {dataSource === "grafana" ? (
        <GrafanaEmbeddedPanels timeRange={timeRange} />
      ) : (
        <div className="grid grid-cols-2 gap-4">
          {/* Row 1: RPS + Error Rate */}
          <Card>
            <CardContent className="pt-4">
              {loading ? (
                <div className="h-[250px] flex items-center justify-center text-muted-foreground">
                  Loading...
                </div>
              ) : (
                <ApiRpsChart data={metricsData?.api_rps || []} />
              )}
            </CardContent>
          </Card>
          <Card>
            <CardContent className="pt-4">
              {loading ? (
                <div className="h-[200px] flex items-center justify-center text-muted-foreground">
                  Loading...
                </div>
              ) : (
                <ErrorRateChart data={metricsData?.error_rates || []} />
              )}
            </CardContent>
          </Card>

          {/* Row 2: Latency + Active Users */}
          <Card>
            <CardContent className="pt-4">
              <LatencyChart data={metricsData?.latency || []} />
            </CardContent>
          </Card>
          <Card>
            <CardContent className="pt-4">
              <ActiveUsersChart stats={metricsData?.active_users || {
                daily: 0, weekly: 0, monthly: 0
              }} />
            </CardContent>
          </Card>

          {/* Row 3: Graph Growth + Token Usage (span 2) */}
          <Card>
            <CardContent className="pt-4">
              <GraphGrowthChart data={metricsData?.graph_growth || []} />
            </CardContent>
          </Card>
          <Card>
            <CardContent className="pt-4">
              <TokenUsageChart data={metricsData?.token_usage || {
                model_breakdown: [],
                estimated_cost: 0,
                total_tokens: 0,
              }} />
            </CardContent>
          </Card>

          {/* Row 4: Worker Queue (span 2) */}
          <Card className="col-span-2">
            <CardContent className="pt-4">
              <WorkerQueueChart data={metricsData?.worker_queue || []} />
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}

function getTimeRange(range: string): [Date, Date] {
  const end = new Date();
  const ms = {
    "1h": 60 * 60 * 1000,
    "6h": 6 * 60 * 60 * 1000,
    "24h": 24 * 60 * 60 * 1000,
    "7d": 7 * 24 * 60 * 60 * 1000,
    "30d": 30 * 24 * 60 * 60 * 1000,
  }[range] || 24 * 60 * 60 * 1000;

  return [new Date(end.getTime() - ms), end];
}
```

---

## 5. Grafana Embedded Panels

### 5.1 When to Use Grafana Embedding

Use Grafana embedded panels when:
- The LGTM stack (Loki, Grafana, Tempo, Mimir) is deployed alongside OpenZep
- You want richer visualisation options (annotations, alert thresholds, ad-hoc queries)
- You already have Grafana dashboards created (see `12-observability/04-grafana-dashboards.md`)

### 5.2 Grafana Embed Component

```typescript
// src/components/analytics/grafana-embed.tsx
"use client";

import { useAuth } from "@/hooks/use-auth";

interface GrafanaEmbedProps {
  dashboardUid: string;
  panelId: number;
  timeRange: string;
  width?: string;
  height?: string;
}

export function GrafanaEmbeddedPanels({ timeRange }: { timeRange: string }) {
  const grafanaUrl = process.env.NEXT_PUBLIC_GRAFANA_URL || "http://grafana:3000";
  const orgId = "1"; // Grafana org ID

  // Map time range to Grafana's from/to
  const rangeMap: Record<string, { from: string; to: string }> = {
    "1h": { from: "now-1h", to: "now" },
    "6h": { from: "now-6h", to: "now" },
    "24h": { from: "now-24h", to: "now" },
    "7d": { from: "now-7d", to: "now" },
    "30d": { from: "now-30d", to: "now" },
  };

  const range = rangeMap[timeRange] || rangeMap["24h"];

  // Dashboard UIDs and panel IDs (set these when creating Grafana dashboards)
  const panels = [
    { dashboardUid: "openzep_api", panelId: 1, title: "API Request Rate" },
    { dashboardUid: "openzep_api", panelId: 2, title: "Error Rate" },
    { dashboardUid: "openzep_api", panelId: 3, title: "Context Latency" },
    { dashboardUid: "openzep_usage", panelId: 1, title: "Active Users" },
    { dashboardUid: "openzep_usage", panelId: 2, title: "Graph Nodes Growth" },
    { dashboardUid: "openzep_usage", panelId: 3, title: "Token Usage" },
    { dashboardUid: "openzep_worker", panelId: 1, title: "Worker Queue Depth" },
  ];

  return (
    <div className="grid grid-cols-2 gap-4">
      {panels.map((panel) => (
        <div key={panel.panelId} className="border rounded-lg overflow-hidden">
          <iframe
            src={`${grafanaUrl}/d-solo/${panel.dashboardUid}/OpenZep?orgId=${orgId}&from=${range.from}&to=${range.to}&panelId=${panel.panelId}&theme=light`}
            width="100%"
            height="300"
            frameBorder="0"
            title={panel.title}
          />
        </div>
      ))}
    </div>
  );
}
```

### 5.3 Grafana Authentication

For embedded iframes to work without login, configure Grafana with anonymous access or use an API key in the URL:

```ini
# grafana.ini
[auth.anonymous]
enabled = true
org_name = Main Org
org_role = Viewer
```

Or use an embedded service account token (more secure):
```
https://grafana.example.com/d-solo/abc123?orgId=1&apiKey=glsa_...
```

### 5.4 Grafana Dashboard JSON

Create these dashboards in Grafana (see `12-observability/04-grafana-dashboards.md` for the complete JSON definitions):

| Dashboard | UID | Panels |
|-----------|-----|--------|
| OpenZep API | `openzep_api` | RPS, Error rate, Latency |
| OpenZep Usage | `openzep_usage` | Active users, Graph growth, Token usage |
| OpenZep Worker | `openzep_worker` | Queue depth, Task throughput |

---

## 6. Recharts vs Grafana Decision Guide

| Criterion | Recharts | Grafana Embed |
|-----------|----------|---------------|
| **Setup effort** | Moderate (build charts) | Low (iframe) |
| **Visual quality** | Good | Excellent |
| **Interactivity** | Basic (tooltips, zoom) | Full (drill-down, annotations) |
| **Data source** | Mimir proxy or backend stats | Mimir directly |
| **Requires LGTM** | No | Yes |
| **Authentication** | Dashboard JWT | Grafana auth (anonymous or API key) |
| **Alerting** | No | Yes (Grafana alerts) |
| **Export** | Screenshot | PNG, CSV, API |
| **Customisation** | Full control | Constrained by dashboard JSON |

### Recommendation

| Scenario | Use |
|----------|-----|
| LGTM stack deployed | **Grafana embed** — richer visualisation, existing dashboards, alerting |
| Standalone dashboard | **Recharts** — self-contained, no external dependency |
| Both available | Recharts as default, Grafana as optional toggle (as coded above) |

---

## 7. Metrics Data Shape

### 7.1 Prometheus Data Format

```typescript
// Prometheus query_range response
interface PromResponse {
  status: "success" | "error";
  data: {
    resultType: "matrix";
    result: Array<{
      metric: Record<string, string>;
      values: Array<[number, string]>;  // [timestamp, value]
    }>;
  };
}

// Transformation before passing to Recharts
function transformPromToRecharts(
  promData: PromResponse,
  valueKey: string
): Array<Record<string, any>> {
  const series = promData.data.result;
  const timeMap = new Map<number, Record<string, any>>();

  for (const s of series) {
    const label = s.metric.endpoint_group || s.metric.model || "value";
    for (const [ts, val] of s.values) {
      if (!timeMap.has(ts)) {
        timeMap.set(ts, { timestamp: new Date(ts * 1000).toISOString() });
      }
      timeMap.get(ts)![label] = parseFloat(val);
    }
  }

  return Array.from(timeMap.values()).sort(
    (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
  );
}
```

---

## 8. Alert Thresholds (Visual Indicators)

Add threshold lines to charts to make it obvious when metrics exceed acceptable ranges:

```typescript
// In latency chart — SRS targets:
// p50 ≤ 50ms (green), p95 ≤ 300ms (amber), p99 ≤ 1500ms (red)

const THRESHOLDS = [
  { label: "p50 Target (50ms)", value: 0.05, color: "#10B981" },
  { label: "p95 Target (300ms)", value: 0.3, color: "#F59E0B" },
  { label: "p99 Target (1500ms)", value: 1.5, color: "#EF4444" },
];

// Render as ReferenceLine components in Recharts:
// <ReferenceLine y={0.05} stroke="#10B981" strokeDasharray="4 4" label="p50 target" />
// <ReferenceLine y={0.3} stroke="#F59E0B" strokeDasharray="4 4" label="p95 target" />
```

---

## 9. Testing

```typescript
// __tests__/components/analytics/api-rps-chart.test.tsx
import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { ApiRpsChart } from "@/components/analytics/api-rps-chart";

describe("ApiRpsChart", () => {
  const sampleData = [
    { timestamp: "2026-06-04T10:00:00Z", memory: 5.2, context: 3.1, search: 1.0, graph: 0.5, admin: 0.2 },
    { timestamp: "2026-06-04T10:05:00Z", memory: 6.0, context: 4.2, search: 1.2, graph: 0.6, admin: 0.3 },
  ];

  it("renders chart with data", () => {
    const { container } = render(<ApiRpsChart data={sampleData} />);
    expect(container.querySelector(".recharts-wrapper")).toBeTruthy();
  });

  it("renders legend labels", () => {
    const { getByText } = render(<ApiRpsChart data={sampleData} />);
    expect(getByText("Memory")).toBeInTheDocument();
    expect(getByText("Context")).toBeInTheDocument();
  });
});
```

---

## 10. Open Questions

| # | Question | Decision |
|---|----------|----------|
| Q1 | Should we expose per-org analytics to org admins (not just super admins)? | Defer — Phase 5. For MVP, analytics is super-admin only. |
| Q2 | Should we support CSV/JSON export of chart data? | Yes — add export button to each chart that calls the underlying PromQL query and downloads as CSV. |
| Q3 | Should we add cost alerts (e.g., "token usage exceeded $100/day")? | Yes — implement as Grafana alert rules. If using Recharts only, add a notification banner when cost exceeds threshold. |
| Q4 | What's the data retention period for metrics? | Mimir default: 30 days. Longer retention requires Mimir's长期 storage (S3/GCS). |

---

*Corresponding SRS requirements: DASH-06, SRS §11.4. Previous: [03-user-graph-explorer.md](03-user-graph-explorer.md).*
