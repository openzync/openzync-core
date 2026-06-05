# CI/CD Pipeline

| | |
|---|---|
| **Document** | 14-testing/06-ci-pipeline.md |
| **Phase** | 5 — Hardening |
| **Author** | Technical Writing |
| **Status** | Draft |

---

## 1. Overview

OpenZep uses **GitLab CI** (self-hosted, TheLinkAI standard). The pipeline is organized into 9 stages with progressive gating: fast checks run first, expensive checks gate merges, and scheduled jobs run nightly/weekly.

### Pipeline Flow

```mermaid
flowchart LR
    A[Push] --> B[Stage 1: lint]
    B --> C[Stage 2: test-unit]
    C --> D[Stage 3: test-integration]
    D --> E[Stage 4: build]
    E --> F[Stage 5: test-e2e]
    F --> G[Stage 6: test-security]
    G --> H[Stage 7: sdk]
    H --> I[Stage 8: publish]
    I --> J[Done]

    K[Scheduled] --> L[Stage 9: performance]
    K --> M[Stage 9: eval]
```

| Stage | Duration (est.) | Parallel | Trigger |
|---|---|---|---|
| 1. `lint` | < 2 min | Yes (4 linters in parallel) | Every branch push |
| 2. `test-unit` | < 3 min | Yes (4 workers) | Every branch push |
| 3. `test-integration` | < 5 min | Sequential (testcontainers) | Every branch push |
| 4. `build` | < 3 min | Yes (4 Docker images in parallel) | Every branch push |
| 5. `test-e2e` | < 8 min | Sequential (Docker Compose) | Merge to `main` |
| 6. `test-security` | < 2 min | Sequential | Merge to `main` |
| 7. `sdk` | < 5 min | Yes (Python + TS + Go in parallel) | Merge to `main` |
| 8. `publish` | < 5 min | Sequential (versioned tags) | Tagged commit (`v*`) |
| 9. `performance` + `eval` | < 20 min | Sequential (nightly only) | Scheduled (nightly/weekly) |

**Per-commit max time:** < 15 minutes (lint + unit + integration + build)
**Merge-to-main max time:** < 30 minutes (all except perf + eval)
**Tag max time:** < 35 minutes (all stages)

---

## 2. Stage Definitions

### 2.1 Stage 1: `lint`

```yaml
# .gitlab-ci.yml
stages:
  - lint
  - test-unit
  - test-integration
  - build
  - test-e2e
  - test-security
  - sdk
  - publish
  - performance

variables:
  PIP_CACHE_DIR: "$CI_PROJECT_DIR/.cache/pip"
  NPM_CACHE_DIR: "$CI_PROJECT_DIR/.cache/npm"
  GOPATH: "$CI_PROJECT_DIR/.cache/go"
  DOCKER_BUILDKIT: "1"
  COMPOSE_DOCKER_CLI_BUILD: "1"

lint:ruff:
  stage: lint
  image: python:3.11-slim
  script:
    - pip install ruff
    - ruff check services/ packages/ tests/ --output-format=gitlab
  artifacts:
    reports:
      ruff: gl-code-quality-report.json
    when: always
  cache:
    key: $CI_COMMIT_REF_SLUG
    paths:
      - .cache/pip

lint:mypy:
  stage: lint
  image: python:3.11-slim
  script:
    - pip install mypy
    - mypy services/ packages/core/ --strict
  allow_failure: true  # Informational only — mypy strict is aspirational
  cache:
    key: $CI_COMMIT_REF_SLUG
    paths:
      - .cache/pip

lint:eslint:
  stage: lint
  image: node:20-alpine
  script:
    - cd apps/dashboard
    - npm ci
    - npm run lint
  cache:
    key: $CI_COMMIT_REF_SLUG
    paths:
      - .cache/npm

lint:prettier:
  stage: lint
  image: node:20-alpine
  script:
    - cd apps/dashboard
    - npm ci
    - npx prettier --check .
  allow_failure: true  # Formatting is auto-fixable, not blocking
  cache:
    key: $CI_COMMIT_REF_SLUG
    paths:
      - .cache/npm
```

### 2.2 Stage 2: `test-unit`

```yaml
test:unit:
  stage: test-unit
  image: python:3.11-slim
  before_script:
    - pip install -r services/api/requirements.txt -r services/api/requirements-dev.txt
    - pip install -e packages/core
  script:
    - pytest tests/unit/
        -n auto
        -m "unit"
        --timeout=30
        --cov=services
        --cov=packages/core
        --cov-report=term
        --cov-report=html:coverage_html
        --junitxml=reports/unit.xml
  artifacts:
    reports:
      junit: reports/unit.xml
    paths:
      - coverage_html/
    when: always
  cache:
    key: $CI_COMMIT_REF_SLUG
    paths:
      - .cache/pip
  coverage: '/^TOTAL.*\s+(\d+)%$/'
```

### 2.3 Stage 3: `test-integration`

```yaml
test:integration:
  stage: test-integration
  image: python:3.11-slim
  services:
    - name: pgvector/pgvector:pg15
      alias: postgres
    - name: redis:7-alpine
      alias: redis
    - name: falkordb/falkordb:latest
      alias: falkordb
  variables:
    DATABASE_URL: "postgresql+asyncpg://postgres:postgres@postgres:5432/testdb"
    REDIS_URL: "redis://redis:6379/0"
    FALKORDB_URL: "redis://falkordb:6380"
    LLM_BACKEND: "mock"
    OPENAI_API_KEY: "sk-test-blocked"
  before_script:
    - pip install -r services/api/requirements.txt -r services/api/requirements-dev.txt
    - pip install -e packages/core
    # Run migrations
    - alembic upgrade head
  script:
    - pytest tests/integration/
        -m "integration"
        --timeout=120
        --junitxml=reports/integration.xml
  artifacts:
    reports:
      junit: reports/integration.xml
    when: always
  cache:
    key: $CI_COMMIT_REF_SLUG
    paths:
      - .cache/pip
```

**⚠️ Important:** Integration tests use **GitLab service containers** (not testcontainers) in CI. Testcontainers are used for local development. The `services:` block above provides the same containers. The application code detects this via environment variables and skips the `testcontainers` setup.

To support both local and CI environments, the conftest.py detects the environment:

```python
# conftest.py — detect CI vs local
@pytest.fixture(scope="session")
def database_url() -> str:
    """Use CI service containers when available, fall back to testcontainers."""
    if os.environ.get("CI") == "true":
        return os.environ["DATABASE_URL"]
    # Local: use testcontainers
    return _start_postgres_container()
```

### 2.4 Stage 4: `build`

```yaml
build:api:
  stage: build
  image: docker:27-cli
  services:
    - docker:dind
  script:
    - docker build -t $CI_REGISTRY_IMAGE/api:$CI_COMMIT_SHORT_SHA -f services/api/Dockerfile .
    - docker tag $CI_REGISTRY_IMAGE/api:$CI_COMMIT_SHORT_SHA $CI_REGISTRY_IMAGE/api:latest
    - docker push $CI_REGISTRY_IMAGE/api:$CI_COMMIT_SHORT_SHA
  parallel:
    matrix:
      - SERVICE: [api, worker, mcp]
        DOCKERFILE:
          - api: services/api/Dockerfile
          - worker: services/worker/Dockerfile
          - mcp: services/mcp/Dockerfile

build:dashboard:
  stage: build
  image: docker:27-cli
  services:
    - docker:dind
  script:
    - docker build -t $CI_REGISTRY_IMAGE/dashboard:$CI_COMMIT_SHORT_SHA -f apps/dashboard/Dockerfile .
    - docker push $CI_REGISTRY_IMAGE/dashboard:$CI_COMMIT_SHORT_SHA
```

### 2.5 Stage 5: `test-e2e`

**Only runs on merge to `main` and tagged commits.**

```yaml
test:e2e:
  stage: test-e2e
  image: docker:27-cli
  services:
    - docker:dind
  variables:
    DOCKER_HOST: tcp://docker:2375
  script:
    # Start stack
    - docker compose -f infra/docker-compose.yml up -d --wait --wait-timeout 120

    # Wait for health checks
    - docker compose exec api curl -f http://localhost:8000/health || exit 1

    # Run E2E tests
    - docker compose exec -T api pytest tests/e2e/
        -m "e2e"
        --timeout=300
        --junitxml=reports/e2e.xml

    # Collect logs on failure
    - docker compose logs --tail=100 > reports/docker-compose-logs.txt

    # Teardown
    - docker compose down -v
  artifacts:
    reports:
      junit: reports/e2e.xml
    paths:
      - reports/docker-compose-logs.txt
    when: always
  rules:
    - if: $CI_COMMIT_BRANCH == "main"
    - if: $CI_COMMIT_TAG =~ /^v\d+\.\d+\.\d+/
```

### 2.6 Stage 6: `test-security`

```yaml
test:security:
  stage: test-security
  image: python:3.11-slim
  services:
    - name: pgvector/pgvector:pg15
      alias: postgres
    - name: redis:7-alpine
      alias: redis
  variables:
    DATABASE_URL: "postgresql+asyncpg://postgres:postgres@postgres:5432/testdb"
    REDIS_URL: "redis://redis:6379/0"
  before_script:
    - pip install -r services/api/requirements.txt -r services/api/requirements-dev.txt
    - pip install bandit semgrep
  script:
    # SAST
    - bandit -r services/ packages/core/ -f json -o reports/bandit.json || true
    - semgrep --config=auto services/ --json -o reports/semgrep.json || true

    # Cross-tenant isolation tests
    - alembic upgrade head
    - pytest tests/security/
        -m "security"
        --timeout=60
        --junitxml=reports/security.xml
        -v
  artifacts:
    reports:
      junit: reports/security.xml
      bandit: reports/bandit.json
      semgrep: reports/semgrep.json
    when: always
  rules:
    - if: $CI_COMMIT_BRANCH == "main"
    - if: $CI_COMMIT_TAG =~ /^v\d+\.\d+\.\d+/
```

### 2.7 Stage 7: `sdk`

```yaml
test:sdk-python:
  stage: sdk
  image: python:3.11-slim
  script:
    - cd packages/sdk-python
    - pip install -e ".[dev]"
    - pytest tests/ --junitxml=reports/sdk-python.xml
  artifacts:
    reports:
      junit: reports/sdk-python.xml
    when: always
  rules:
    - if: $CI_COMMIT_BRANCH == "main"
    - if: $CI_COMMIT_TAG =~ /^v\d+\.\d+\.\d+/

test:sdk-typescript:
  stage: sdk
  image: node:20-alpine
  script:
    - cd packages/sdk-typescript
    - npm ci
    - npm run build
    - npm test -- --reporter junit --reporter-options output=reports/sdk-ts.xml
  artifacts:
    reports:
      junit: reports/sdk-ts.xml
    when: always
  rules:
    - if: $CI_COMMIT_BRANCH == "main"
    - if: $CI_COMMIT_TAG =~ /^v\d+\.\d+\.\d+/

test:sdk-go:
  stage: sdk
  image: golang:1.22-alpine
  script:
    - cd packages/sdk-go
    - go test ./... -v -count=1 2>&1 | tee reports/sdk-go.txt
  artifacts:
    paths:
      - reports/sdk-go.txt
    when: always
  rules:
    - if: $CI_COMMIT_BRANCH == "main"
    - if: $CI_COMMIT_TAG =~ /^v\d+\.\d+\.\d+/
```

### 2.8 Stage 8: `publish`

**Only runs on tagged commits matching `v*`.**

```yaml
publish:pypi:
  stage: publish
  image: python:3.11-slim
  script:
    - cd packages/sdk-python
    - pip install build twine
    - python -m build
    - twine upload --repository-url $PYPI_REPOSITORY dist/*
  rules:
    - if: $CI_COMMIT_TAG =~ /^v\d+\.\d+\.\d+/
  needs:
    - test:sdk-python
    - test:unit
    - test:integration

publish:npm:
  stage: publish
  image: node:20-alpine
  script:
    - cd packages/sdk-typescript
    - npm ci
    - npm publish
  rules:
    - if: $CI_COMMIT_TAG =~ /^v\d+\.\d+\.\d+/
  needs:
    - test:sdk-typescript

publish:docker:
  stage: publish
  image: docker:27-cli
  services:
    - docker:dind
  script:
    # Tag and push all images with semver tag
    - |
      for img in api worker mcp dashboard; do
        docker tag $CI_REGISTRY_IMAGE/$img:$CI_COMMIT_SHORT_SHA \
          $CI_REGISTRY_IMAGE/$img:$CI_COMMIT_TAG
        docker tag $CI_REGISTRY_IMAGE/$img:$CI_COMMIT_SHORT_SHA \
          $CI_REGISTRY_IMAGE/$img:latest
        docker push $CI_REGISTRY_IMAGE/$img:$CI_COMMIT_TAG
        docker push $CI_REGISTRY_IMAGE/$img:latest
      done
  rules:
    - if: $CI_COMMIT_TAG =~ /^v\d+\.\d+\.\d+/
  needs:
    - build:api
    - build:dashboard

publish:go:
  stage: publish
  image: golang:1.22-alpine
  script:
    - cd packages/sdk-go
    - git tag $CI_COMMIT_TAG
    - git push origin $CI_COMMIT_TAG
    # Go modules use tagged repo — pkg.go.dev auto-indexes
  rules:
    - if: $CI_COMMIT_TAG =~ /^v\d+\.\d+\.\d+/
  needs:
    - test:sdk-go
```

### 2.9 Stage 9: `performance`

**Scheduled — not per-commit.**

```yaml
performance:load:
  stage: performance
  image: python:3.11-slim
  script:
    - pip install locust
    # Deploy the app stack
    - docker compose -f infra/docker-compose.yml up -d --wait --wait-timeout 120
    # Seed data
    - docker compose exec -T api python tests/performance/seed_data.py
    # Run load test
    - locust -f tests/performance/locustfile.py
        --headless
        --host http://api:8000
        --users 500
        --spawn-rate 20
        --run-time 570s
        --csv=results/loadtest
        --html=results/report.html
        --expect-workers=1
    # Archive results
    - tar czf results.tar.gz results/
  artifacts:
    paths:
      - results/
    when: always
  rules:
    - if: $CI_PIPELINE_SOURCE == "schedule"
      when: always
    - when: never

performance:eval:
  stage: performance
  image: python:3.11-slim
  script:
    - pip install -r services/api/requirements.txt -r services/api/requirements-dev.txt
    - pip install -e packages/core
    - docker compose -f infra/docker-compose.yml up -d --wait --wait-timeout 120
    - pytest tests/evals/ -m "eval" --timeout=600 --junitxml=reports/eval.xml
  artifacts:
    reports:
      junit: reports/eval.xml
    when: always
  rules:
    - if: $CI_PIPELINE_SOURCE == "schedule"
      when: always
    - when: never
```

---

## 3. Caching Strategy

```yaml
cache:
  key: "$CI_COMMIT_REF_SLUG"
  paths:
    - .cache/pip/
    - .cache/npm/
    - .cache/go/
    - .cache/docker/
  policy: pull-push
```

| Cache | Directory | Used By | Size (approx.) |
|---|---|---|---|
| pip | `.cache/pip/` | Python stages | 200 MB |
| npm | `.cache/npm/` | Dashboard + TS SDK | 150 MB |
| Go mod | `.cache/go/` | Go SDK | 100 MB |
| Docker layers | `.cache/docker/` | Build stage | 500 MB |

**Cache invalidation:** Cache key is `$CI_COMMIT_REF_SLUG` (branch name). This means branches share cache until a new branch is created. Full cache flush when `requirements.txt`, `package.json`, or `go.mod` changes.

---

## 4. Artifacts

```yaml
# Global artifact retention policy
artifacts:
  expire_in: 30 days
  when: always
```

| Artifact | Stage | Retention | Use |
|---|---|---|---|
| `reports/unit.xml` | test-unit | 30 days | Test results dashboard |
| `reports/integration.xml` | test-integration | 30 days | Test results dashboard |
| `reports/e2e.xml` | test-e2e | 30 days | Test results dashboard |
| `reports/security.xml` | test-security | 30 days | Security audit trail |
| `reports/bandit.json` | test-security | 90 days | SAST trend analysis |
| `reports/semgrep.json` | test-security | 90 days | SAST trend analysis |
| `reports/docker-compose-logs.txt` | test-e2e | 7 days | Debugging on failure only |
| `reports/sdk-python.xml` | sdk | 30 days | SDK test results |
| `coverage_html/` | test-unit | 7 days | Coverage report |
| `results/` (CSV + HTML) | performance | 90 days | Performance trend analysis |

---

## 5. Pipeline Triggers

| Event | Stages Run | Max Duration |
|---|---|---|
| **Branch push** (`feature/*`, `fix/*`, `chore/*`) | lint, test-unit, test-integration, build | < 15 min |
| **Merge to `main`** | lint, test-unit, test-integration, build, test-e2e, test-security, sdk | < 30 min |
| **Tag push** (`v1.2.3`) | All except performance + eval, plus publish | < 35 min |
| **Scheduled (nightly, 02:00 UTC)** | performance (load + eval) | < 30 min |
| **Scheduled (weekly, Sunday 04:00 UTC)** | Full eval suite + coverage report | < 45 min |
| **Manual trigger (via GitLab UI/API)** | Any stage(s) | Configurable |

### 5.1 Rules Configuration

```yaml
workflow:
  rules:
    - if: $CI_PIPELINE_SOURCE == "schedule"
      variables:
        SCHEDULED_PIPELINE: "true"
    - if: $CI_COMMIT_TAG =~ /^v\d+\.\d+\.\d+/
      variables:
        RELEASE_PIPELINE: "true"
    - if: $CI_COMMIT_BRANCH == "main"
      variables:
        MAIN_BRANCH_PIPELINE: "true"
    - if: $CI_MERGE_REQUEST_IID
    - if: $CI_COMMIT_BRANCH
```

### 5.2 Perf-Impact PR Label Automation

```yaml
# .gitlab/ci/rules.yml
.perf-impact:
  rules:
    - changes:
        - services/**/context_service.py
        - services/**/retrieval_service.py
        - core/rrf.py
        - repositories/**/episode_repository.py
        - dependencies/cache.py
        - prompts/**/*.jinja2
        - alembic/versions/*
      when: always
```

When a PR modifies these paths, the pipeline auto-adds the `perf:impact` label and runs a quick smoke load test:

```yaml
test:quick-load:
  stage: test-security  # Reuse same stage for gating
  script:
    - locust -f tests/performance/locustfile.py
        --headless
        --host http://api:8000
        --users 100
        --spawn-rate 10
        --run-time 60s
        --csv=results/quick
        --html=results/quick.html
    # Assert p99 < 500ms threshold
    - python -c "
        import csv; import sys
        with open('results/quick_stats.csv') as f:
            rows = list(csv.DictReader(f))
            for r in rows:
                if 'GET /context' in r['Name']:
                    p99 = float(r['99%%'])
                    assert p99 < 500, f'p99 {p99}ms > 500ms threshold'
                    print(f'Quick load OK: p99={p99}ms')
        "
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
      changes:
        - services/**/context_service.py
        - services/**/retrieval_service.py
        - core/rrf.py
```

---

## 6. Environment Management

### 6.1 CI Variables

| Variable | Scope | Description |
|---|---|---|
| `$CI_REGISTRY_IMAGE` | Project | Docker registry path |
| `$PYPI_REPOSITORY` | Project | PyPI upload URL (test or production) |
| `$PYPI_TOKEN` | Project (masked) | PyPI API token |
| `$NPM_TOKEN` | Project (masked) | npm publish token |
| `$DOCKER_HUB_USERNAME` | Project (masked) | Docker Hub username |
| `$DOCKER_HUB_TOKEN` | Project (masked) | Docker Hub access token |

### 6.2 Test Environment Overrides

```yaml
# In each test stage, override environment-specific variables:
variables:
  LOG_LEVEL: ERROR
  CONTEXT_CACHE_TTL: "10"  # Short cache for faster test iteration
  MAX_WORKERS: "2"
  SECRET_KEY: "ci-secret-key-not-for-production"
```

---

## 7. Failure Handling

| Scenario | Behaviour |
|---|---|
| Linter fails | Pipeline stops immediately (fail-fast). Commit must be fixed. |
| Unit test fails | Pipeline stops. Test report shows which file + line. |
| Integration test fails | Full test report artifact saved. Containers are still torn down. |
| E2E test fails | Docker Compose logs saved as artifact. Stuck containers killed via `down -v --timeout=30`. |
| Security test fails (SAST) | Pipeline continues (allowed to fail). Security report is generated. |
| Security test fails (cross-tenant) | Pipeline stops immediately. This is a security boundary violation. |
| Build fails | Pipeline stops. Docker build cache may need clearing. |
| Publish fails | Pipeline is marked as failed but previous stages' artifacts are preserved. Manual retry via GitLab UI. |
| Performance test fails | Pipeline continues (informational). Report saved for analysis. |

### 7.1 Retry Policy

```yaml
test:integration:
  retry:
    max: 2
    when:
      - runner_system_failure
      - stuck_or_timeout_failure
      - scheduler_failure

performance:load:
  retry:
    max: 1
    when:
      - runner_system_failure
```

Test failures (assertion errors) are never retried — they indicate real defects.

---

## 8. Local Pre-Commit Hook

For developer convenience, a pre-commit hook ensures basic quality before pushing:

```bash
# .githooks/pre-commit (install with: git config core.hooksPath .githooks)
#!/bin/bash
set -e

echo "=== Lint ==="
ruff check services/ packages/ tests/

echo "=== Format ==="
ruff format --check services/ packages/ tests/

echo "=== Unit Tests (quick) ==="
pytest tests/unit/ -m "unit" -n auto --timeout=15 -q

echo "✅ Pre-commit checks passed"
```

Install with:

```bash
git config core.hooksPath .githooks
```

---

## 9. Dependencies Between Stages

```yaml
# Ensure stages run in the correct order and only when needed
needs:
  # Build depends on tests passing
  build:api:
    needs: ["test:unit", "test:integration"]

  # E2E depends on build artifacts being available
  test:e2e:
    needs: ["build:api", "build:dashboard"]

  # Security depends on build (needs running app)
  test:security:
    needs: ["build:api"]

  # SDK tests don't depend on build (they test against mock API)
  test:sdk-python:
    needs: ["test:unit"]
  test:sdk-typescript:
    needs: []
  test:sdk-go:
    needs: []

  # Publish depends on everything passing
  publish:pypi:
    needs: ["test:sdk-python", "test:security", "test:e2e"]
  publish:npm:
    needs: ["test:sdk-typescript", "test:security", "test:e2e"]
  publish:docker:
    needs: ["test:e2e", "test:security"]
  publish:go:
    needs: ["test:sdk-go", "test:security", "test:e2e"]
```

---

## 10. Monitoring Pipeline Health

| Metric | Alert Threshold | Action |
|---|---|---|
| Pipeline duration > 15 min (per-commit) | Warning | Investigate if new tests or services were added |
| Pipeline duration > 30 min (merge-to-main) | Warning | Consider parallelizing stages |
| Pipeline failure rate > 5% in a week | Critical | Flaky test triage; may need `@pytest.mark.flaky` |
| Average container startup time > 30s | Warning | Testcontainers or Docker cache may be degraded |
| Coverage drop > 2% | Warning | PR description must include coverage justification |
