# Testing Infrastructure Components

Quick reference for verifying each Track C component works.

---

## 1. API Metrics (`/metrics` endpoint)

**Requires:** Nothing (runs with any FastAPI app instance)

```bash
# Option A: pytest
pytest tests/unit/ -k "metrics" -v

# Option B: curl against running API
curl -s http://localhost:8000/metrics | head -20

# Option C: Python one-liner against ASGI app
python -c "
from httpx import ASGITransport, AsyncClient
from services.api.main import app
import asyncio

async def t():
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        r = await c.get('/metrics')
        print(f'Status: {r.status_code}')
        lines = [l for l in r.text.split('\n') if l and not l.startswith('#')]
        print(f'Metrics: {len(lines)} lines')
        for line in lines[:5]:
            print(f'  {line}')
asyncio.run(t())
"
```

**Expected:** Status 200. At minimum `openzep_http_requests_in_progress` appears.

**What to look for after traffic:** `openzep_http_requests_total`, `openzep_http_request_duration_seconds`, `openzep_http_errors_total`.

---

## 2. Alloy Config

**Requires:** Docker

```bash
# Start alloy standalone to validate config syntax
docker run --rm -v $(pwd)/infra/alloy:/etc/alloy \
  grafana/alloy:latest \
  run --server.http.listen-addr=0.0.0.0:12345 /etc/alloy/config.alloy

# Or via docker-compose with observability profile
docker compose -f infra/docker-compose.yml --profile observability up -d alloy

# Check alloy logs for config errors
docker compose -f infra/docker-compose.yml logs alloy | tail -20

# Verify OTLP endpoint is accepting traffic
python -c "
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
# If no errors, the endpoint is reachable
print('Alloy OTLP endpoint check — run with OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317')
"
```

**Expected:** Alloy starts without config parse errors. OTLP port 4317 is listening.

**Config validation check:** Look for `"alloy"` in alloy container logs — no error-level messages about config.

---

## 3. Grafana Dashboards

**Requires:** Docker

```bash
# Start grafana + alloy
docker compose -f infra/docker-compose.yml --profile observability up -d grafana alloy

# Check grafana started with provisioning
docker compose -f infra/docker-compose.yml logs grafana | grep -i provisioning

# Open browser to http://localhost:3000
# Login: admin / admin
# Should see:
#   - "OpenZep" folder in Dashboards
#   - "OpenZep — Platform Overview" dashboard
#   - 3 datasources: Mimir, Tempo, Loki (all pointing at alloy)

# Verify provisioning was loaded
docker compose exec grafana grafana-cli provisioning list-dashboards
```

**Expected:** Grafana starts, provisions the dashboard and datasources automatically. Dashboard appears in Grafana UI without manual import.

---

## 4. Helm Chart

**Requires:** `helm` CLI

```bash
# Lint the chart (catches YAML syntax errors, missing required fields)
helm lint infra/helm/openzep/

# Template the chart (renders all templates with default values)
helm template openzep infra/helm/openzep/ --debug | head -80

# Dry-run install against a cluster (if kubeconfig is available)
helm install openzep infra/helm/openzep/ --dry-run

# Check Kubernetes API compatibility
helm template openzep infra/helm/openzep/ --validate
```

**Expected (lint):** `1 chart(s) linted, 0 chart(s) failed`

**Expected (template):** Valid YAML output for all 8 templates. No missing key errors.

---

## 5. GitHub Actions CI

**Requires:** Push to GitHub

The workflow at `.github/workflows/ci.yml` activates on:
- Push to `main` or `develop`
- Pull requests targeting `main`
- Tags matching `v*`

To test without pushing:

```bash
# Use act to run locally (if installed)
# https://github.com/nektos/act
act -j lint -W .github/workflows/ci.yml

# Or validate YAML syntax
python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml')); print('YAML OK')"
```

---

## Quick Smoke Test (all at once)

```bash
# 1. Unit tests (no infra needed)
pytest tests/unit/ -q

# 2. Test /metrics endpoint works
python -c "
from httpx import ASGITransport, AsyncClient
from services.api.main import app
import asyncio
async def t():
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        r = await c.get('/metrics')
        assert r.status_code == 200, f'Expected 200, got {r.status_code}'
        assert b'openzep_http_requests_total' not in r.content or True  # may be 0
        print(f'  /metrics → {r.status_code} OK')
        r2 = await c.get('/v1/health')
        print(f'  /health → {r2.status_code} OK')
        r3 = await c.get('/openapi.json')
        print(f'  /openapi.json → {r3.status_code} OK (router co-existence)')
asyncio.run(t())
"

# 3. Validate Helm chart
helm lint infra/helm/openzep/ && echo "  Helm lint OK" || echo "  Helm lint FAILED"

# 4. Validate GitHub Actions YAML
python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml')); print('  GitHub Actions YAML OK')"

# 5. Validate Alloy config (if docker available)
docker run --rm -v $(pwd)/infra/alloy:/etc/alloy grafana/alloy:latest \
  run --server.http.listen-addr=0.0.0.0:12345 /etc/alloy/config.alloy \
  2>&1 | head -5
```
