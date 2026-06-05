# LLM Cost Control

> **Phase:** 2 (Full Feature Parity) — P1 requirement
> **SRS References:** OQ-03 (LLM cost at scale), PERF-05 (30s extraction SLA), WRK-04 (queue metrics)
> **Index:** 5.7 — Read after all NLP pipeline docs; cross-cuts 5.1–5.6

---

## 1. Overview

LLM API costs are the single largest operational expense for OpenZep at scale. Without controls, a single organization processing 100K messages/day could incur $500+/day in LLM costs (entity + fact + classification calls per message).

This document defines the cost control system:

- **Per-org daily token budgets** — hard caps that stop enrichment when exceeded
- **Enrichment depth levels** — `none` / `basic` / `full` controlling LLM calls per message
- **Per-endpoint enable/disable** — granular control over which NLP tasks run
- **Token accounting** — every LLM call logged to `llm_usage` table
- **Webhook alerts** — notified at 80% and 100% of budget
- **Ollama as default** — self-hosted local LLM eliminates API costs
- **Cost projection** — estimated costs per 100K messages at each depth

---

## 2. Per-Org Daily Token Budget

### 2.1 Storage

The daily token budget is stored in each organization's `quotas` JSONB field:

```json
{
  "daily_llm_budget_tokens": 1000000,
  "enrichment_depth": "basic",
  "enabled_tasks": {
    "entity_extraction": true,
    "fact_extraction": true,
    "classification": false,
    "structured_extraction": true
  },
  "pii": {
    "mode": "redact",
    "pii_types": ["email", "phone"]
  }
}
```

### 2.2 Budget Check

```python
# packages/core/cost/budget_checker.py

import structlog
from datetime import date
from uuid import UUID

logger = structlog.get_logger(__name__)


class BudgetExceededError(Exception):
    """Raised when a task is skipped due to budget limits."""
    pass


class BudgetChecker:
    """Checks daily LLM token budgets before executing enrichment tasks.

    Budget is tracked per-org, per-day in the llm_usage table.
    The checker queries total tokens consumed today and compares
    against the org's daily budget.
    """

    def __init__(self, db_session_factory, redis=None) -> None:
        self._db = db_session_factory
        self._redis = redis

    async def check_budget(self, org_id: UUID, task_type: str) -> bool:
        """Check if the org has budget remaining for LLM calls.

        Args:
            org_id: Organization to check.
            task_type: The NLP task requesting the budget check.

        Returns:
            True if budget is available, False if exceeded.
        """
        # Get org quotas
        quotas = await self._get_org_quotas(org_id)
        daily_budget = quotas.get("daily_llm_budget_tokens")

        if daily_budget is None or daily_budget <= 0:
            # No budget configured = unlimited (opt-in to cost control)
            return True

        # Check if this task is enabled for the org
        enabled_tasks = quotas.get("enabled_tasks", {})
        if task_type in enabled_tasks and not enabled_tasks[task_type]:
            logger.info(
                "cost_control.task_disabled",
                org_id=str(org_id),
                task_type=task_type,
            )
            return False

        # Query today's total token consumption
        today = date.today()
        used = await self._get_todays_tokens(org_id, today)

        available = daily_budget - used
        if available <= 0:
            logger.warning(
                "cost_control.budget_exceeded",
                org_id=str(org_id),
                task_type=task_type,
                daily_budget=daily_budget,
                used_today=used,
            )
            return False

        # Log remaining budget at 80% threshold
        usage_ratio = used / daily_budget
        if usage_ratio >= 0.80:
            await self._maybe_trigger_alert(
                org_id=org_id,
                usage_ratio=usage_ratio,
                used=used,
                budget=daily_budget,
            )

        return True

    async def record_usage(
        self,
        org_id: UUID,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        task_type: str,
        episode_id: UUID | None = None,
        session_id: UUID | None = None,
    ) -> None:
        """Record an LLM call's token usage.

        Inserts a row into the llm_usage table for budget tracking
        and cost projection.

        Args:
            org_id: Organization that owns this LLM call.
            model: Model identifier (e.g., 'gpt-4o-mini', 'ollama/llama3').
            prompt_tokens: Number of prompt tokens consumed.
            completion_tokens: Number of completion tokens consumed.
            task_type: Which NLP task triggered this call.
            episode_id: Source episode (for traceability).
            session_id: Source session (for traceability).
        """
        cost_estimate = self._estimate_cost(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

        async with self._db() as session:
            await session.execute(
                text("""
                    INSERT INTO llm_usage
                        (organization_id, model, prompt_tokens, completion_tokens,
                         task_type, cost_estimate, episode_id, session_id)
                    VALUES
                        (:org_id, :model, :prompt_tokens, :completion_tokens,
                         :task_type, :cost_estimate, :episode_id, :session_id)
                """),
                {
                    "org_id": org_id,
                    "model": model,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "task_type": task_type,
                    "cost_estimate": cost_estimate,
                    "episode_id": episode_id,
                    "session_id": session_id,
                },
            )
            await session.commit()

    async def _get_todays_tokens(self, org_id: UUID, day: date) -> int:
        """Sum of all tokens (prompt + completion) consumed today for an org."""
        async with self._db() as session:
            result = await session.execute(
                text("""
                    SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0)
                    FROM llm_usage
                    WHERE organization_id = :org_id
                      AND DATE(created_at) = :day
                """),
                {"org_id": org_id, "day": day},
            )
            return result.scalar_one()

    async def _get_org_quotas(self, org_id: UUID) -> dict:
        """Get the organization's quotas JSONB."""
        async with self._db() as session:
            result = await session.execute(
                text("SELECT quotas FROM organizations WHERE id = :org_id"),
                {"org_id": org_id},
            )
            row = result.scalar_one_or_none()
            return row or {}

    def _estimate_cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        """Estimate cost in USD for this LLM call.

        Uses published per-token pricing. Returns 0 for local models.
        """
        # Per-1K-token pricing (input / output)
        MODEL_PRICING = {
            "gpt-4o": (0.0025, 0.01),
            "gpt-4o-mini": (0.00015, 0.0006),
            "gpt-4-turbo": (0.01, 0.03),
            "claude-3.5-sonnet": (0.003, 0.015),
            "claude-3-haiku": (0.00025, 0.00125),
        }
        pricing = MODEL_PRICING.get(model)
        if pricing is None:
            # Local models (Ollama) have no API cost
            if model.startswith("ollama/"):
                return 0.0
            # Unknown model — log warning and return 0
            logger.warning("cost_control.unknown_model_pricing", model=model)
            return 0.0

        input_cost = (prompt_tokens / 1000) * pricing[0]
        output_cost = (completion_tokens / 1000) * pricing[1]
        return round(input_cost + output_cost, 6)

    async def _maybe_trigger_alert(self, org_id: UUID, usage_ratio: float, used: int, budget: int) -> None:
        """Trigger webhook alert if configured."""
        org = await self._get_org_quotas(org_id)
        webhook_url = org.get("budget_alert_webhook")

        if not webhook_url:
            return

        threshold = "80%" if usage_ratio < 1.0 else "100%"
        import httpx
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(webhook_url, json={
                    "event": "budget_alert",
                    "org_id": str(org_id),
                    "threshold": threshold,
                    "tokens_used": used,
                    "tokens_budget": budget,
                    "usage_ratio": usage_ratio,
                    "timestamp": datetime.utcnow().isoformat(),
                })
        except Exception as e:
            logger.warning(
                "cost_control.webhook_failed",
                org_id=str(org_id),
                webhook_url=webhook_url,
                error=str(e),
            )
```

---

## 3. LLM Usage Table

```sql
CREATE TABLE llm_usage (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id   UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    model             TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    task_type         TEXT NOT NULL,          -- entity_extraction, fact_extraction, etc.
    cost_estimate     FLOAT8 DEFAULT 0.0,     -- estimated USD cost
    episode_id        UUID,                   -- optional reference
    session_id        UUID,                   -- optional reference
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_llm_usage_org_date ON llm_usage (organization_id, DATE(created_at));
CREATE INDEX idx_llm_usage_task_type ON llm_usage (organization_id, task_type);

-- Daily summary view for dashboards
CREATE VIEW daily_llm_usage AS
SELECT
    organization_id,
    DATE(created_at) AS day,
    task_type,
    model,
    SUM(prompt_tokens) AS total_prompt_tokens,
    SUM(completion_tokens) AS total_completion_tokens,
    SUM(prompt_tokens + completion_tokens) AS total_tokens,
    SUM(cost_estimate) AS total_cost
FROM llm_usage
GROUP BY organization_id, DATE(created_at), task_type, model;
```

---

## 4. Enrichment Depth Levels

Each organization has an `enrichment_depth` setting with three levels:

| Level | LLM Calls per Message | Description | Avg Tokens per Message | When to Use |
|-------|----------------------|-------------|------------------------|-------------|
| `none` | 0 | Raw storage only. No NLP enrichment. No LLM calls. | 0 | Budget-constrained, test environments, high-volume |
| `basic` | 1 | Entity extraction only (most critical for graph quality). | ~1,000 | Production default — balances cost and value |
| `full` | 3-4 | Entities + facts + classification + structured extraction. | ~3,500 | Premium orgs, maximum enrichment |

### 4.1 What Runs at Each Level

```python
# packages/core/cost/depth.py

from enum import Enum


class EnrichmentDepth(str, Enum):
    NONE = "none"
    BASIC = "basic"
    FULL = "full"


# Task → depth level map
DEPTH_REQUIREMENTS: dict[str, EnrichmentDepth] = {
    "entity_extraction": EnrichmentDepth.BASIC,
    "fact_extraction": EnrichmentDepth.FULL,
    "classification": EnrichmentDepth.FULL,
    "structured_extraction": EnrichmentDepth.FULL,
}


def is_task_allowed(
    task_type: str,
    org_depth: str,
    enabled_tasks: dict[str, bool],
) -> bool:
    """Check if a task is allowed for an org given its depth and per-task toggles.

    Args:
        task_type: The NLP task type.
        org_depth: The org's enrichment_depth setting.
        enabled_tasks: Dict of per-task enable/disable overrides.

    Returns:
        True if the task should run.
    """
    # Check per-task toggle first
    if task_type in enabled_tasks and not enabled_tasks[task_type]:
        return False

    try:
        org_level = EnrichmentDepth(org_depth)
    except ValueError:
        org_level = EnrichmentDepth.BASIC  # safe default

    required = DEPTH_REQUIREMENTS.get(task_type, EnrichmentDepth.FULL)

    # Order: none < basic < full
    level_order = {
        EnrichmentDepth.NONE: 0,
        EnrichmentDepth.BASIC: 1,
        EnrichmentDepth.FULL: 2,
    }

    return level_order[org_level] >= level_order[required]
```

### 4.2 Integration with Worker Tasks

```python
# Inside each worker task handler, before the LLM call:

from packages.core.cost.depth import is_task_allowed

async def extract_entities(ctx, task_input):
    quotas = await get_org_quotas(task_input.org_id)

    if not is_task_allowed(
        task_type="entity_extraction",
        org_depth=quotas.get("enrichment_depth", "basic"),
        enabled_tasks=quotas.get("enabled_tasks", {}),
    ):
        logger.info(
            "task.skipped.depth_level",
            task="entity_extraction",
            org_id=str(task_input.org_id),
            depth=quotas.get("enrichment_depth"),
        )
        return {"status": "skipped", "reason": "depth_level"}
```

---

## 5. Per-Endpoint Enable/Disable

Organizations can independently enable or disable each NLP task:

```json
{
  "enabled_tasks": {
    "entity_extraction": true,
    "fact_extraction": false,
    "classification": false,
    "structured_extraction": true
  }
}
```

When a task is disabled:
- The ARQ worker receives the job but immediately returns `skipped`
- No token budget is consumed
- The task is logged at INFO level for monitoring

---

## 6. Webhook Budget Alerts

### 6.1 Configuration

Optional webhook URL stored in `organizations.quotas`:

```json
{
  "budget_alert_webhook": "https://hooks.slack.com/services/...",
  "daily_llm_budget_tokens": 500000
}
```

### 6.2 Alert Payload

```json
POST <webhook_url>
Content-Type: application/json

{
  "event": "budget_alert",
  "org_id": "org_abc123",
  "threshold": "80%",
  "tokens_used": 400000,
  "tokens_budget": 500000,
  "usage_ratio": 0.8,
  "timestamp": "2026-06-05T14:30:00Z"
}
```

Two events fire:
1. **80% threshold** — warning, enrichment still runs
2. **100% threshold** — enrichment stops, tasks return `skipped`

---

## 7. Token Accounting

Every LLM call across every worker task must go through `BudgetChecker.record_usage()`:

```python
# In every worker task, after successful LLM call:
await ctx["cost_controller"].record_usage(
    org_id=input_data.org_id,
    model=response["model"],
    prompt_tokens=response["usage"]["prompt_tokens"],
    completion_tokens=response["usage"]["completion_tokens"],
    task_type="entity_extraction",  # or fact_extraction, classification, etc.
    episode_id=input_data.episode_id,
)
```

**Enforcement:** Any worker task that calls the LLM without calling `record_usage` will be caught in code review. The `LLMClient` wrapper should enforce this:

```python
# Option: instrument the LLMClient to auto-record usage
class InstrumentedLLMClient(LLMClient):
    def __init__(self, cost_controller: BudgetChecker, config: LLMConfig):
        self._cost_controller = cost_controller
        super().__init__(config)

    async def chat_completion(self, ..., meta: dict | None = None):
        response = await super().chat_completion(...)
        if meta:
            await self._cost_controller.record_usage(
                org_id=meta["org_id"],
                model=response["model"],
                prompt_tokens=response["usage"]["prompt_tokens"],
                completion_tokens=response["usage"]["completion_tokens"],
                task_type=meta["task_type"],
                episode_id=meta.get("episode_id"),
            )
        return response
```

---

## 8. Ollama as Default LLM Backend

### 8.1 Configuration

```yaml
# .env or docker-compose
LLM_BACKEND=ollama
OLLAMA_BASE_URL=http://ollama:11434
LLM_MODEL=llama3.1:8b
EMBEDDING_BACKEND=ollama
EMBEDDING_MODEL=nomic-embed-text
```

### 8.2 Cost Comparison

| Backend | Cost per 1M input tokens | Cost per 1M output tokens | Pros | Cons |
|---------|--------------------------|---------------------------|------|------|
| `gpt-4o-mini` | $0.15 | $0.60 | Best quality | API cost, requires internet |
| `gpt-4o` | $2.50 | $10.00 | Highest quality | Expensive |
| Ollama (llama3.1:8b) | $0 | $0 | Zero API cost, air-gapped | Lower accuracy, requires GPU for speed |
| Ollama (llama3.1:70b) | $0 | $0 | Zero API cost, good quality | Requires 40GB+ GPU |

### 8.3 Impact on Token Budgets

When using Ollama, `daily_llm_budget_tokens` can be set to `-1` (unlimited) since there's no API cost. However, rate limits still apply based on GPU capacity.

---

## 9. Cost Projection Table

Estimated costs per **100K messages** at each enrichment depth (using `gpt-4o-mini` pricing):

| Depth Level | LLM Calls per Msg | Est. Tokens per Msg | Cost per 100K Msgs | Monthly (3M msgs) |
|-------------|------------------|---------------------|--------------------|--------------------|
| `none` | 0 | 0 | **$0** | **$0** |
| `basic` (entities only) | 1 | ~1,000 (900 prompt + 100 completion) | **$0.15 × 900 + $0.60 × 10 = $19.50** | **~$585** |
| `full` (entities + facts + classification) | 3-4 | ~3,500 (3,000 prompt + 500 completion) | **$0.15 × 3000 + $0.60 × 50 = $75.00** | **~$2,250** |
| `full` + structured extraction | 4-5 (one per schema per session) | ~5,000 (4,000 prompt + 1,000 completion) | **$0.15 × 4000 + $0.60 × 100 = $120.00** | **~$3,600** |

**With Ollama (llama3.1:8b):** All costs drop to **$0** — only infrastructure cost (GPU electricity).

### 9.1 Budget Recommendations by Org Plan

| Plan | Suggested Daily Budget | Suggested Depth |
|------|----------------------|-----------------|
| Free / Trial | 50,000 tokens/day | `basic` |
| Pro | 500,000 tokens/day | `full` |
| Enterprise | Custom (often unlimited) | `full` |
| Self-hosted Ollama | Unlimited (`-1`) | `full` |

---

## 10. Budget Dashboard (Grafana)

### 10.1 Key Queries

```promql
# Token consumption per org (last 7 days)
sum by (organization_id) (
  memgraph_llm_tokens_total{task_type="entity_extraction"}
)

# Daily spend per org (USD)
sum by (organization_id) (
  memgraph_llm_cost_total
)

# Budget utilization percentage
(
  sum by (organization_id) (
    memgraph_llm_tokens_total
  )
  / on (organization_id)
  memgraph_org_budget_tokens
) * 100
```

### 10.2 Dashboard Panels

| Panel | Metric | Visualization |
|-------|--------|---------------|
| Daily Token Consumption by Org | `llm_usage` table | Bar chart, stacked by task type |
| Budget Utilization % | Per-org used/budget | Gauge (red > 80%) |
| Cost Projection | 7-day avg extrapolated to 30 days | Line chart with overrun date marker |
| Tasks Skipped Due to Budget | Counter from worker logs | Total count, red highlight |
| Per-Task Token Breakdown | `task_type` dimension | Stacked area chart |
| Ollama vs API Cost Savings | Comparison panel | Side-by-side cost estimate |

### 10.3 Alert Rules (Grafana/Mimir)

```yaml
# Budget threshold alert
groups:
  - name: llm_budget
    rules:
      - alert: LLMBudget80Percent
        expr: |
          sum by(organization_id) (
            rate(memgraph_llm_tokens_total[24h])
          ) / on(organization_id) memgraph_org_budget_tokens > 0.8
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Org {{ $labels.organization_id }} at {{ $value | humanizePercentage }} of daily LLM budget"

      - alert: LLMBudgetExceeded
        expr: |
          sum by(organization_id) (
            rate(memgraph_llm_tokens_total[24h])
          ) / on(organization_id) memgraph_org_budget_tokens >= 1.0
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "Org {{ $labels.organization_id }} has exceeded daily LLM budget"
```

---

## 11. Metrics & Observability

### 11.1 Prometheus Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `memgraph_llm_tokens_total` | Counter | `org_id, model, task_type` | Total LLM tokens consumed |
| `memgraph_llm_cost_total` | Counter | `org_id, model` | Estimated USD cost |
| `memgraph_llm_calls_total` | Counter | `org_id, model, task_type, status` | Total LLM API calls |
| `memgraph_tasks_skipped_total` | Counter | `org_id, task_type, reason` | Tasks skipped (budget, depth, disabled) |
| `memgraph_org_budget_tokens` | Gauge | `org_id` | Configured daily token budget |
| `memgraph_budget_utilization_ratio` | Gauge | `org_id` | Current day's usage / budget |

### 11.2 Structured Logging

```python
# Budget exceeded
logger.warning(
    "cost_control.budget_exceeded",
    org_id=str(org_id),
    task_type=task_type,
    daily_budget=daily_budget,
    used_today=used,
    budget_remaining=available,
)

# Task skipped
logger.info(
    "cost_control.task_skipped",
    org_id=str(org_id),
    task_type=task_type,
    reason=f"depth_level ({org_depth})",
)

# LLM call recorded
logger.info(
    "cost_control.usage_recorded",
    org_id=str(org_id),
    model=model,
    task_type=task_type,
    tokens_prompt=prompt_tokens,
    tokens_completion=completion_tokens,
    cost_estimate=cost_estimate,
)
```

---

## 12. Testing Guide

### 12.1 Unit Tests

```python
# tests/unit/cost/test_budget_checker.py

import pytest
from datetime import date
from uuid import uuid4
from packages.core.cost.budget_checker import BudgetChecker


class TestBudgetChecker:

    @pytest.mark.asyncio
    async def test_unlimited_budget_returns_true(self, checker: BudgetChecker) -> None:
        """Org with no budget set should always have budget available."""
        result = await checker.check_budget(uuid4(), "entity_extraction")
        assert result is True

    @pytest.mark.asyncio
    async def test_budget_exceeded_returns_false(self, checker: BudgetChecker) -> None:
        """Org that has exceeded daily budget should return False."""
        org_id = uuid4()
        # Mock: org has budget=1000, already used 1000
        checker._get_org_quotas = lambda o: {"daily_llm_budget_tokens": 1000}
        checker._get_todays_tokens = lambda o, d: 1000
        result = await checker.check_budget(org_id, "entity_extraction")
        assert result is False

    @pytest.mark.asyncio
    async def test_budget_available_returns_true(self, checker: BudgetChecker) -> None:
        """Org under budget should return True."""
        org_id = uuid4()
        checker._get_org_quotas = lambda o: {"daily_llm_budget_tokens": 1000}
        checker._get_todays_tokens = lambda o, d: 300
        result = await checker.check_budget(org_id, "entity_extraction")
        assert result is True

    @pytest.mark.asyncio
    async def test_task_disabled_returns_false(self, checker: BudgetChecker) -> None:
        """Per-task disable should return False regardless of budget."""
        org_id = uuid4()
        checker._get_org_quotas = lambda o: {
            "daily_llm_budget_tokens": 1000000,
            "enabled_tasks": {"entity_extraction": False},
        }
        result = await checker.check_budget(org_id, "entity_extraction")
        assert result is False

    def test_cost_estimate_gpt4o_mini(self, checker: BudgetChecker) -> None:
        """gpt-4o-mini pricing: $0.15/1K input, $0.60/1K output."""
        cost = checker._estimate_cost("gpt-4o-mini", 1000, 100)
        assert cost == pytest.approx((1.0 * 0.15) + (0.1 * 0.60), rel=0.01)

    def test_cost_estimate_ollama(self, checker: BudgetChecker) -> None:
        """Ollama should have zero cost."""
        cost = checker._estimate_cost("ollama/llama3.1", 1000, 100)
        assert cost == 0.0

    def test_cost_estimate_unknown_model(self, checker: BudgetChecker) -> None:
        """Unknown model should return 0.0 and log a warning."""
        cost = checker._estimate_cost("unknown-model", 1000, 100)
        assert cost == 0.0
```

### 12.2 Integration Tests

- Insert llm_usage rows → verify budget check correctly sums them
- Verify webhook fires at 80% budget
- Verify webhook fires at 100% budget
- Depth `none` → all enrichment tasks skipped
- Depth `basic` → entity extraction runs, facts/classification skipped
- Per-task toggle off → specific task skipped

---

## 13. Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_BACKEND` | `openai` | `openai`, `azure`, `ollama` |
| `LLM_MODEL` | `gpt-4o-mini` | Default model for enrichment |
| `DEFAULT_ENRICHMENT_DEPTH` | `basic` | Default depth for new orgs |
| `DEFAULT_DAILY_BUDGET_TOKENS` | `-1` | Default budget (-1 = unlimited) |
| `BUDGET_CHECK_CACHE_TTL` | `60` | Seconds to cache budget check result (Redis) |
| `BUDGET_ALERT_WEBHOOK_TIMEOUT` | `5` | Webhook call timeout in seconds |
| `COST_ESTIMATION_ENABLED` | `true` | Enable/disable cost estimation (adds latency) |

---

## 14. Open Questions

| ID | Question | Status |
|----|----------|--------|
| CC-01 | Should we implement a rolling budget (last 24h sliding window) instead of daily reset? | **Decision:** Daily reset (UTC midnight) is simpler and predictable. Rolling window may surprise orgs. |
| CC-02 | Should budget be shared across all orgs in a deployment (global cap)? | **Decision:** Phase 4 — add optional global cap via `GLOBAL_DAILY_BUDGET_TOKENS` env var. |
| CC-03 | How to handle concurrent LLM calls that collectively exceed budget? | **Decision:** Accept the small overrun — budget check + race is < 60ms. The Cache TTL (60s) reduces this window. |
| CC-04 | Should we charge orgs for failed LLM calls (timeout, invalid response)? | **Decision:** Yes — tokens were still consumed. `record_usage` runs on any non-cached response. |

---

## 15. Related Documents

| Document | Why |
|----------|-----|
| [02-entity-extraction.md](02-entity-extraction.md) | First consumer of budget check and token accounting |
| [03-fact-extraction.md](03-fact-extraction.md) | Same pattern — budget check before LLM call |
| [04-dialog-classification.md](04-dialog-classification.md) | Classification tasks gated by depth level |
| [05-structured-extraction.md](05-structured-extraction.md) | Structured extraction — most expensive per session |
| [02-auth-tenancy/03-tenant-isolation.md](../02-auth-tenancy/03-tenant-isolation.md) | Quotas stored in organizations table |
| [12-observability/04-grafana-dashboards.md](../12-observability/04-grafana-dashboards.md) | Budget dashboard panel definitions |
| [01-prompt-templates.md](01-prompt-templates.md) | Prompt templates determine token consumption per call |
