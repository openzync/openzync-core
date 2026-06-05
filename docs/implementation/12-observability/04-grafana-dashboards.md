# Grafana Dashboards Guide

## Overview

MemGraph ships a pre-built Grafana dashboard providing operational visibility into the platform. The dashboard is provisioned automatically via Grafana Alloy and checked into the repository under `infra/grafana/dashboards/`.

---

## Data Sources

| Data | Storage | Query Language | Grafana Plugin |
|---|---|---|---|
| Metrics | Mimir (via Alloy) | PromQL | Built-in (Prometheus) |
| Traces | Tempo (via Alloy) | TraceQL | Built-in (Tempo) |
| Logs | Loki (via Alloy) | LogQL | Built-in (Loki) |

All data flows through Grafana Alloy on the same host:

```
MemGraph → Alloy (scrape/collect) → Mimir / Tempo / Loki → Grafana
```

### Provisioning (`infra/grafana/datasources/alloy.yaml`)

```yaml
apiVersion: 1
datasources:
  - name: Mimir
    type: prometheus
    url: http://alloy:9090
    access: proxy
    isDefault: true

  - name: Tempo
    type: tempo
    url: http://alloy:3200
    access: proxy

  - name: Loki
    type: loki
    url: http://alloy:3100
    access: proxy
```

---

## Dashboard JSON

### Provisioning File (`infra/grafana/dashboards/memgraph-overview.json`)

The full dashboard JSON is checked into the repo at `infra/grafana/dashboards/memgraph-overview.json`. Auto-provisioned via:

```yaml
apiVersion: 1
providers:
  - name: "MemGraph"
    orgId: 1
    folder: "MemGraph"
    type: file
    disableDeletion: false
    editable: true
    options:
      path: /etc/grafana/provisioning/dashboards/memgraph-overview.json
```

---

## Dashboard Panels

### Panel 1: API Request Rate & Error Rate

**Type**: Stacked area chart (time series)
**Time range**: Last 24 hours
**Data source**: Mimir
**Query**:

```promql
# Request rate by status group
sum(rate(memgraph_http_requests_total[5m])) by (status)

# Error rate percentage
(
  sum(rate(memgraph_http_requests_total{status="5xx"}[5m]))
  /
  sum(rate(memgraph_http_requests_total[5m]))
) * 100
```

**Visual**:
- Area chart, stacked
- `2xx` = green, `4xx` = yellow, `5xx` = red
- Error rate as overlay line (right axis, %)
- Legend: method + path breakdown on hover

**Thresholds**:
- Error rate > 1%: red dashed line

---

### Panel 2: Context Latency Percentiles

**Type**: Time series with percentile lines
**Time range**: Last 24 hours
**Data source**: Mimir
**Query**:

```promql
# p50
histogram_quantile(0.50, sum(rate(memgraph_context_assembly_duration_seconds_bucket[5m])) by (le))

# p95
histogram_quantile(0.95, sum(rate(memgraph_context_assembly_duration_seconds_bucket[5m])) by (le))

# p99
histogram_quantile(0.99, sum(rate(memgraph_context_assembly_duration_seconds_bucket[5m])) by (le))
```

**Visual**:
- Three lines: p50 (solid, thin), p95 (dashed), p99 (bold, red)
- Optional: separate series for `cache_hit=true` vs `cache_hit=false`
- Unit: milliseconds (multiply by 1000 in transform)

**Thresholds**:
- p99 > 1000ms (warning): yellow dashed
- p99 > 2000ms (critical): red dashed

**Alternative (heatmap)**:

```
LEGEND: Heatmap
Query: sum(rate(memgraph_context_assembly_duration_seconds_bucket[5m])) by (le)
```

---

### Panel 3: Worker Queue Depth

**Type**: Time series
**Time range**: Last 24 hours
**Data source**: Mimir
**Query**:

```promql
# High priority queue
memgraph_worker_queue_depth{queue_name="high"}

# Low priority queue
memgraph_worker_queue_depth{queue_name="low"}
```

**Visual**:
- Two lines: high queue (red), low queue (blue)
- Fill below lines with 0.1 opacity
- Unit: count

**Thresholds**:
- Depth > 500: yellow (warning)
- Depth > 1000: red (critical)

---

### Panel 4: Token Usage

**Type**: Bar chart
**Time range**: Last 7 days (daily buckets)
**Data source**: Mimir
**Query**:

```promql
# By model
sum(increase(memgraph_llm_tokens_total[1d])) by (model)

# By org (second query)
sum(increase(memgraph_llm_tokens_total[1d])) by (org_id)
```

**Visual**:
- Stacked bar chart, one bar per day
- Color by `model` or `org_id`
- Two variants in same row:
  1. Tokens by model
  2. Tokens by org

**Repeat**: Row repeated for `llm_tokens` and `embedding_tokens`

---

### Panel 5: Graph Node Growth

**Type**: Cumulative area chart
**Time range**: Last 7 days
**Data source**: Mimir
**Query**:

```promql
# Total entity nodes per org
memgraph_graph_nodes_total

# Growth rate
sum(increase(memgraph_graph_nodes_total[1d])) by (org_id)
```

**Visual**:
- Area chart, cumulative
- One series per `org_id`
- Fill below with 0.1 opacity
- Unit: count

**Alternative**: Table showing:
| Org | Current Nodes | 7d Growth | Growth % |
|---|---|---|---|

---

### Panel 6: Service Health

**Type**: Status grid (Stat panels in a row)
**Time range**: Last 5 minutes
**Data source**: Mimir (or Loki for uptime)
**Query**:

```promql
# API health — up metric or scrape presence
up{job="memgraph_api"}

# Worker health
up{job="memgraph_worker"}

# MCP health
up{job="memgraph_mcp"}

# PostgreSQL
pg_up{service="memgraph"}

# Redis
redis_up{service="memgraph"}

# FalkorDB
falkordb_up{service="memgraph"}
```

**Visual**:
- 6 stat panels in a row
- Green checkmark = healthy (value 1)
- Red X = unhealthy (value 0)
- Show text: "Healthy" / "Unhealthy"

**Alternative**: Use Loki for service logs — if no ERROR logs in 5m, show green:

```logql
count_over_time({service="api"} |= `"level":"CRITICAL"` [5m])
```

---

## Additional Panels (Optional)

### Worker Task Throughput

```promql
# Task rate by type
sum(rate(memgraph_worker_tasks_total[5m])) by (task_type)

# Success vs failure rate
sum(rate(memgraph_worker_tasks_total{status="success"}[5m])) / sum(rate(memgraph_worker_tasks_total[5m]))
```

### DB Connection Pool

```promql
# Active connections
memgraph_db_connections_active{db_name="postgres"}

# Pool utilization
(
  memgraph_db_connections_active{db_name="postgres"}
  /
  (memgraph_db_connections_active{db_name="postgres"} + memgraph_db_connections_idle{db_name="postgres"})
) * 100
```

### Error Breakdown

```promql
# Error rate by code
sum(rate(memgraph_error_code_total[5m])) by (error_code)
```

---

## Dashboard Variables

| Variable | Type | Definition | Usage |
|---|---|---|---|
| `org_id` | Query | `label_values(memgraph_http_requests_total, org_id)` | Filter all panels by org |
| `service` | Query | `label_values(up, job)` | Filter by service |
| `time_range` | Interval | `24h, 7d, 30d` | Quick time range selector |

---

## Provisioning Script

### `scripts/provision-grafana.sh`

```bash
#!/bin/bash
# Run this script to apply Grafana dashboards and datasources.
# Assumes Grafana API is accessible at $GRAFANA_URL with admin credentials.

GRAFANA_URL="${GRAFANA_URL:-http://localhost:3000}"
GRAFANA_USER="${GRAFANA_USER:-admin}"
GRAFANA_PASSWORD="${GRAFANA_PASSWORD:-admin}"

echo "Provisioning Grafana dashboards..."

# Apply datasource
curl -s -X POST "${GRAFANA_URL}/api/datasources" \
  -u "${GRAFANA_USER}:${GRAFANA_PASSWORD}" \
  -H "Content-Type: application/json" \
  -d @infra/grafana/datasources/alloy.json

# Apply dashboard
curl -s -X POST "${GRAFANA_URL}/api/dashboards/db" \
  -u "${GRAFANA_USER}:${GRAFANA_PASSWORD}" \
  -H "Content-Type: application/json" \
  -d "{
    \"dashboard\": $(cat infra/grafana/dashboards/memgraph-overview.json),
    \"overwrite\": true
  }"

echo "Done."
```

---

## File Checklist

Ensure these files exist in the repository:

```
infra/
└── grafana/
    ├── datasources/
    │   └── alloy.yaml              # Data source provisioning
    ├── dashboards/
    │   └── memgraph-overview.json  # Dashboard JSON (generated from Grafana UI)
    └── provisioning.yaml           # Dashboard provider config
```

### Generating the Dashboard JSON

1. Build the dashboard in Grafana UI using the queries above.
2. Export as JSON: Share → Export → Download JSON.
3. Save to `infra/grafana/dashboards/memgraph-overview.json`.
4. Remove any datasource-specific UIDs (use generic names matched to provisioning).
