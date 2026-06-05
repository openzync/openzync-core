# Kubernetes Helm Chart Guide

## Overview

OpenZep ships a Helm chart for production Kubernetes deployment. The chart covers all components: API, worker, MCP server, PostgreSQL, FalkorDB, Redis with Sentinel, PgBouncer, the admin dashboard, and Grafana Alloy.

---

## Chart Structure

```
infra/helm/OpenZep/
├── Chart.yaml                  # Chart metadata
├── values.yaml                 # All configurable parameters (documented)
├── values.dev.yaml             # Dev overrides (smaller resources, single replicas)
├── values.prod.yaml            # Production overrides (HA settings)
└── templates/
    ├── _helpers.tpl            # Named templates
    ├── secret.yaml             # External Secrets / Sealed Secrets
    ├── configmap.yaml          # App config
    ├── deployment-api.yaml     # API deployment
    ├── service-api.yaml        # API service
    ├── hpa-api.yaml            # API HPA
    ├── deployment-worker.yaml  # Worker deployment
    ├── hpa-worker.yaml         # Worker HPA
    ├── deployment-mcp.yaml     # MCP deployment
    ├── service-mcp.yaml        # MCP service
    ├── statefulset-postgres.yaml
    ├── service-postgres.yaml
    ├── statefulset-falkordb.yaml
    ├── service-falkordb.yaml
    ├── statefulset-redis.yaml
    ├── deployment-pgbouncer.yaml
    ├── service-pgbouncer.yaml
    ├── deployment-dashboard.yaml
    ├── service-dashboard.yaml
    ├── daemonset-alloy.yaml
    ├── ingress.yaml
    ├── networkpolicy.yaml
    ├── pdb.yaml                # PodDisruptionBudgets
    ├── pvc.yaml                # PersistentVolumeClaims
    └── tests/
        └── test-connection.yaml
```

---

## Chart.yaml

```yaml
apiVersion: v2
name: OpenZep
description: OpenZep — Open-Source Agent Memory Platform
type: application
version: 0.1.0
appVersion: "0.1.0"
keywords:
  - agent-memory
  - knowledge-graph
  - open-source
home: https://github.com/thelinkai/OpenZep
sources:
  - https://github.com/thelinkai/OpenZep
maintainers:
  - name: TheLinkAI
    email: engineering@thelinkai.com
```

---

## values.yaml

```yaml
# ─── Global Settings ──────────────────────────────────────────
global:
  environment: production
  logLevel: INFO
  imageRegistry: ghcr.io/thelinkai
  imageTag: "0.1.0"
  imagePullPolicy: Always

# ─── API Service ───────────────────────────────────────────────
api:
  enabled: true
  replicas: 2
  image:
    repository: OpenZep-api
    tag: ""
    pullPolicy: ""
  service:
    type: ClusterIP
    port: 8000
  resources:
    requests:
      cpu: "500m"
      memory: "512Mi"
    limits:
      cpu: "2"
      memory: "1Gi"
  hpa:
    enabled: true
    minReplicas: 2
    maxReplicas: 10
    targetCPUUtilizationPercentage: 70
    targetMemoryUtilizationPercentage: 70
  podDisruptionBudget:
    minAvailable: 1
  env:
    - name: DATABASE_URL
      valueFrom:
        secretKeyRef:
          name: OpenZep-secrets
          key: database_url
    - name: REDIS_URL
      value: "redis-sentinel://OpenZep-redis-sentinel:26379/mymaster/0"
    - name: FALKORDB_URL
      value: "redis://OpenZep-falkordb:6380"
    - name: OTEL_EXPORTER_OTLP_ENDPOINT
      value: "http://OpenZep-alloy:4317"
    - name: LOG_LEVEL
      value: "INFO"
    - name: ENVIRONMENT
      valueFrom:
        fieldRef:
          fieldPath: metadata.namespace
  readinessProbe:
    httpGet:
      path: /ready
      port: 8000
    initialDelaySeconds: 30
    periodSeconds: 15
    timeoutSeconds: 5
    failureThreshold: 3
  livenessProbe:
    httpGet:
      path: /health
      port: 8000
    initialDelaySeconds: 30
    periodSeconds: 15
    timeoutSeconds: 5
    failureThreshold: 3

# ─── Worker ────────────────────────────────────────────────────
worker:
  enabled: true
  replicas: 2
  image:
    repository: OpenZep-worker
    tag: ""
    pullPolicy: ""
  resources:
    requests:
      cpu: "500m"
      memory: "512Mi"
    limits:
      cpu: "2"
      memory: "1Gi"
  hpa:
    enabled: true
    minReplicas: 2
    maxReplicas: 20
    metrics:
      - type: External
        external:
          metric:
            name: memgraph_worker_queue_depth
            selector:
              matchLabels:
                queue_name: high
          target:
            type: AverageValue
            averageValue: 500
  podDisruptionBudget:
    minAvailable: 1
  env:
    - name: DATABASE_URL
      valueFrom:
        secretKeyRef:
          name: OpenZep-secrets
          key: database_url
    - name: REDIS_URL
      value: "redis-sentinel://OpenZep-redis-sentinel:26379/mymaster/0"
    - name: FALKORDB_URL
      value: "redis://OpenZep-falkordb:6380"
    - name: LOG_LEVEL
      value: "INFO"

# ─── MCP Server ────────────────────────────────────────────────
mcp:
  enabled: true
  replicas: 1
  image:
    repository: OpenZep-mcp
    tag: ""
    pullPolicy: ""
  service:
    type: ClusterIP
    port: 8001
  resources:
    requests:
      cpu: "250m"
      memory: "256Mi"
    limits:
      cpu: "1"
      memory: "512Mi"

# ─── PostgreSQL ────────────────────────────────────────────────
postgresql:
  enabled: true
  image: pgvector/pgvector:pg15
  replicas: 1  # Use Patroni or pg_auto_failover for HA
  storage:
    size: 100Gi
    storageClass: standard
  resources:
    requests:
      cpu: "1"
      memory: "1Gi"
    limits:
      cpu: "4"
      memory: "4Gi"
  config:
    max_connections: 100
    shared_buffers: "1GB"
    effective_cache_size: "3GB"
    work_mem: "64MB"
    maintenance_work_mem: "256MB"
    wal_level: replica
    max_wal_senders: 3
    wal_keep_size: "1GB"

# ─── PgBouncer ─────────────────────────────────────────────────
pgbouncer:
  enabled: true
  replicas: 1
  image: bitnami/pgbouncer:latest
  poolMode: transaction
  defaultPoolSize: 25
  maxClientConn: 100
  resources:
    requests:
      cpu: "100m"
      memory: "128Mi"
    limits:
      cpu: "500m"
      memory: "256Mi"

# ─── FalkorDB ──────────────────────────────────────────────────
falkordb:
  enabled: true
  image: falkordb/falkordb:latest
  storage:
    size: 50Gi
    storageClass: standard
  resources:
    requests:
      cpu: "500m"
      memory: "512Mi"
    limits:
      cpu: "2"
      memory: "2Gi"

# ─── Redis ─────────────────────────────────────────────────────
redis:
  enabled: true
  image: redis:7-alpine
  sentinel:
    enabled: true
    quorum: 2
  master:
    storage:
      size: 10Gi
    resources:
      requests:
        cpu: "250m"
        memory: "256Mi"
      limits:
        memory: "1Gi"
  replicas:
    count: 2
    storage:
      size: 10Gi

# ─── Dashboard ─────────────────────────────────────────────────
dashboard:
  enabled: true
  image:
    repository: OpenZep-dashboard
    tag: ""
    pullPolicy: ""
  service:
    type: ClusterIP
    port: 3000
  env:
    - name: NEXT_PUBLIC_API_URL
      value: "https://api.OpenZep.example.com"
  resources:
    requests:
      cpu: "250m"
      memory: "256Mi"
    limits:
      cpu: "1"
      memory: "512Mi"

# ─── Grafana Alloy ─────────────────────────────────────────────
alloy:
  enabled: true
  image: grafana/alloy:latest
  configFile: /etc/alloy/config.alloy
  resources:
    requests:
      cpu: "200m"
      memory: "256Mi"
    limits:
      cpu: "1"
      memory: "512Mi"

# ─── Ingress ──────────────────────────────────────────────────
ingress:
  enabled: true
  className: traefik  # or nginx
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
  hosts:
    - host: api.OpenZep.example.com
      serviceName: OpenZep-api
      servicePort: 8000
    - host: dashboard.OpenZep.example.com
      serviceName: OpenZep-dashboard
      servicePort: 3000
  tls:
    - secretName: OpenZep-tls
      hosts:
        - api.OpenZep.example.com
        - dashboard.OpenZep.example.com

# ─── Network Policies ─────────────────────────────────────────
networkPolicy:
  enabled: true

# ─── External Secrets ─────────────────────────────────────────
externalSecrets:
  enabled: true
  backend: aws  # aws, gcp, azure, vault
  # For AWS Secrets Manager:
  # secretStore:
  #   region: us-east-1
  # secrets:
  #   - name: OpenZep-secrets
  #     keys:
  #       - database_url
  #       - jwt_secret
  #       - openai_api_key
```

---

## Component Templates

### API Deployment (Key Sections)

```yaml
# templates/deployment-api.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "OpenZep.fullname" . }}-api
  labels:
    app: OpenZep
    component: api
spec:
  replicas: {{ .Values.api.replicas }}
  selector:
    matchLabels:
      app: OpenZep
      component: api
  template:
    metadata:
      labels:
        app: OpenZep
        component: api
    spec:
      containers:
        - name: api
          image: "{{ .Values.global.imageRegistry }}/{{ .Values.api.image.repository }}:{{ .Values.api.image.tag | default .Values.global.imageTag }}"
          imagePullPolicy: {{ .Values.api.image.pullPolicy | default .Values.global.imagePullPolicy }}
          ports:
            - containerPort: 8000
              name: http
          env:
            {{- toYaml .Values.api.env | nindent 12 }}
          readinessProbe:
            {{- toYaml .Values.api.readinessProbe | nindent 12 }}
          livenessProbe:
            {{- toYaml .Values.api.livenessProbe | nindent 12 }}
          resources:
            {{- toYaml .Values.api.resources | nindent 12 }}
---
# templates/hpa-api.yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ include "OpenZep.fullname" . }}-api
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {{ include "OpenZep.fullname" . }}-api
  minReplicas: {{ .Values.api.hpa.minReplicas }}
  maxReplicas: {{ .Values.api.hpa.maxReplicas }}
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: {{ .Values.api.hpa.targetCPUUtilizationPercentage }}
    - type: Resource
      resource:
        name: memory
        target:
          type: Utilization
          averageUtilization: {{ .Values.api.hpa.targetMemoryUtilizationPercentage }}
```

### Worker HPA with Custom Metrics

```yaml
# templates/hpa-worker.yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ include "OpenZep.fullname" . }}-worker
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {{ include "OpenZep.fullname" . }}-worker
  minReplicas: {{ .Values.worker.hpa.minReplicas }}
  maxReplicas: {{ .Values.worker.hpa.maxReplicas }}
  metrics:
    - type: External
      external:
        metric:
          name: memgraph_worker_queue_depth
          selector:
            matchLabels:
              queue_name: "high"
        target:
          type: AverageValue
          averageValue: {{ .Values.worker.hpa.metrics[0].external.target.averageValue }}
```

**Note**: The custom metric requires a Prometheus Adapter configured to expose `memgraph_worker_queue_depth` from Mimir.

### PodDisruptionBudget

```yaml
# templates/pdb.yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: {{ include "OpenZep.fullname" . }}-api-pdb
spec:
  minAvailable: {{ .Values.api.podDisruptionBudget.minAvailable }}
  selector:
    matchLabels:
      app: OpenZep
      component: api
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: {{ include "OpenZep.fullname" . }}-worker-pdb
spec:
  minAvailable: {{ .Values.worker.podDisruptionBudget.minAvailable }}
  selector:
    matchLabels:
      app: OpenZep
      component: worker
```

### NetworkPolicy

```yaml
# templates/networkpolicy.yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{ include "OpenZep.fullname" . }}-default-deny
spec:
  podSelector: {}
  policyTypes:
    - Ingress
    - Egress
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{ include "OpenZep.fullname" . }}-api-allow
spec:
  podSelector:
    matchLabels:
      app: OpenZep
      component: api
  ingress:
    - from:
        - namespaceSelector: {}  # Ingress controller namespace
      ports:
        - port: 8000
  policyTypes:
    - Ingress
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{ include "OpenZep.fullname" . }}-inter-service
spec:
  podSelector:
    matchLabels:
      app: OpenZep
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app: OpenZep
      ports:
        - port: 8000    # API
        - port: 8001    # MCP
        - port: 5432    # PostgreSQL
        - port: 6432    # PgBouncer
        - port: 6379    # Redis
        - port: 6380    # FalkorDB
        - port: 26379   # Redis Sentinel
        - port: 3000    # Dashboard
  egress:
    - to:
        - podSelector:
            matchLabels:
              app: OpenZep
  policyTypes:
    - Ingress
    - Egress
```

### Ingress (Traefik)

```yaml
# templates/ingress.yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {{ include "OpenZep.fullname" . }}
  annotations:
    {{- toYaml .Values.ingress.annotations | nindent 4 }}
spec:
  ingressClassName: {{ .Values.ingress.className }}
  tls:
    {{- toYaml .Values.ingress.tls | nindent 4 }}
  rules:
    {{- range .Values.ingress.hosts }}
    - host: {{ .host }}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: {{ .serviceName }}
                port:
                  number: {{ .servicePort }}
    {{- end }}
```

---

## Persistent Volumes

| Component | Size | Storage Class | Access Mode |
|---|---|---|---|
| PostgreSQL | 100Gi | standard | ReadWriteOnce |
| FalkorDB | 50Gi | standard | ReadWriteOnce |
| Redis Master | 10Gi | standard | ReadWriteOnce |
| Redis Replica (×2) | 10Gi each | standard | ReadWriteOnce |

```yaml
# templates/pvc.yaml
{{- if .Values.postgresql.enabled }}
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {{ include "OpenZep.fullname" . }}-postgres
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: {{ .Values.postgresql.storage.size }}
  storageClassName: {{ .Values.postgresql.storage.storageClass }}
{{- end }}
```

---

## Secrets Management

### Option A: External Secrets Operator (Recommended)

```yaml
# templates/secret.yaml (External Secrets)
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: OpenZep-secrets
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: aws-secretsmanager
    kind: SecretStore
  target:
    name: OpenZep-secrets
  data:
    - secretKey: database_url
      remoteRef:
        key: /OpenZep/prod/database_url
    - secretKey: jwt_secret
      remoteRef:
        key: /OpenZep/prod/jwt_secret
    - secretKey: openai_api_key
      remoteRef:
        key: /OpenZep/prod/openai_api_key
```

### Option B: Sealed Secrets

```yaml
# templates/secret.yaml (Sealed Secrets)
apiVersion: bitnami.com/v1alpha1
kind: SealedSecret
metadata:
  name: OpenZep-secrets
spec:
  encryptedData:
    database_url: AgBy3i4...  # sealed with kubeseal
    jwt_secret: AgBd7f9...
    openai_api_key: AgXk2m1...
```

---

## Installing the Chart

```bash
# Add the repo
helm repo add OpenZep https://charts.thelinkai.com
helm repo update

# Install with dev values
helm install OpenZep OpenZep/OpenZep \
  --namespace OpenZep \
  --create-namespace \
  --values infra/helm/OpenZep/values.dev.yaml

# Install with production values
helm install OpenZep OpenZep/OpenZep \
  --namespace OpenZep \
  --create-namespace \
  --values infra/helm/OpenZep/values.prod.yaml

# Upgrade
helm upgrade OpenZep OpenZep/OpenZep \
  --values infra/helm/OpenZep/values.prod.yaml

# Local development (from repo root)
helm install OpenZep ./infra/helm/OpenZep \
  --namespace OpenZep \
  --create-namespace
```

---

## Verification

```bash
# Check all pods are running
kubectl get pods -n OpenZep

# Check services
kubectl get svc -n OpenZep

# Test API health
kubectl port-forward -n OpenZep svc/OpenZep-api 8000:8000
curl http://localhost:8000/health

# Check HPA status
kubectl get hpa -n OpenZep

# Check PDB status
kubectl get pdb -n OpenZep

# View logs
kubectl logs -n OpenZep -l component=api
```

---

## Resource Sizing Guide

| Deployment Size | API | Worker | Postgres | Redis | FalkorDB |
|---|---|---|---|---|---|
| Small (< 10 orgs) | 2 × 1CPU/512MB | 2 × 1CPU/512MB | 2CPU/4GB/100GB | 1GB | 2CPU/2GB/50GB |
| Medium (10-100 orgs) | 4 × 2CPU/1GB | 4 × 2CPU/1GB | 4CPU/8GB/250GB | 2GB | 4CPU/4GB/100GB |
| Large (100+ orgs) | 8 × 4CPU/2GB | 8 × 4CPU/2GB | 8CPU/16GB/500GB | 4GB | 8CPU/8GB/250GB |
