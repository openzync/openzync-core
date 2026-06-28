Solid catches — every single one is right. Let me update the plan with corrections.

**Confirmed: `idx_facts_temporal` (model says `ix_fact_user_valid_range` but the migration actually creates `idx_facts_temporal`) is a btree index on `(user_id, valid_from, valid_to)`.** It's useless for `&&` range overlap. The model and migration name mismatch is a separate papercut.

---

## Revised Final Plan

### Phase 1 — Fix `valid_to` Bug on Relationship Upsert

**SQL change** — `packages/graph_backend/postgres.py:440-451`:

```sql
ON CONFLICT (source_id, target_id, relationship_type)
WHERE invalid_at IS NULL
DO UPDATE SET
    properties = CAST(:properties AS jsonb),
    fact = :fact,
    confidence = GREATEST(graph_relationships.confidence, :confidence),
    valid_from = LEAST(graph_relationships.valid_from, COALESCE(:valid_from, now())),
    valid_to = CASE                                              -- FIXED
        WHEN :valid_to IS NULL THEN NULL                        -- new fact is open-ended, open the range
        WHEN graph_relationships.valid_to IS NULL THEN NULL     -- already open, stay open
        ELSE GREATEST(graph_relationships.valid_to, :valid_to)  -- both closed, expand
    END,
    updated_at = now()
```

**Tests** — add all four cases from the table in the original plan, plus these additional edge cases from the fix:

| Test | Assertion |
|---|---|
| `test_valid_to_null_reopens_closed_range` | Existing with `valid_to=2024-06`, upsert with `valid_to=NULL` → `valid_to=NULL` |
| `test_valid_to_closed_over_null` | Existing with `valid_to=NULL`, upsert with `valid_to=2024-06` → `valid_to=2024-06` (first close) |
| `test_valid_to_both_null_stays_null` | Both NULL → NULL |

---

### Phase 2 — Temporal Conflict Prevention for Facts

#### 2a — Schema Migration

Pre-migration data integrity check (same as before):
```sql
SELECT count(*) FROM facts f1 WHERE EXISTS (
  SELECT 1 FROM facts f2
  WHERE f2.source_episode_id = f1.source_episode_id
    AND f2.subject = f1.subject AND f2.predicate = f1.predicate AND f2."object" = f1."object"
    AND f2.id != f1.id AND f2.invalid_at IS NULL
    AND tstzrange(f2.valid_from, f2.valid_to, '[)') &&
        tstzrange(f1.valid_from, f1.valid_to, '[)')
);
```

**Migration**:

1. `CREATE EXTENSION IF NOT EXISTS btree_gist`
2. `DROP CONSTRAINT uq_facts_episode_triple`
3. Add exclusion constraint — same SQL as before, with `COALESCE` to ±infinity
4. `CREATE INDEX IF NOT EXISTS ix_facts_not_invalidated ON facts (...) WHERE invalid_at IS NULL`

**Risk assessment on open-ended facts**: In `batch_create_or_skip()` (the extraction worker path), every fact in a single batch gets `valid_from=now()` and `valid_to=NULL`. The in-memory `_deduplicate_facts()` in `extract_facts.py:621-688` filters identical triples before they reach the DB — so a single extraction pass won't produce two identical open-ended facts for the same episode. Across different episodes, `source_episode_id` differs, so no conflict. **The exclusion constraint is sound for the extraction pipeline.** Test this explicitly.

#### 2b — Repository Changes

**Removed `batch_upsert()` from the plan.** No concrete caller today = dead code. Instead:

- `batch_create()` — add optional `on_conflict: Literal["error", "skip"] = "error"` parameter. When `"skip"`, uses `ON CONFLICT DO NOTHING`.
- `batch_create_or_skip()` — unchanged (range-aware via the exclusion constraint).

#### 2c — Caller Audit

No changes needed. Extraction worker calls `batch_create_or_skip` (first-writer-wins). API ingest calls `batch_create` (strict).

---

### Phase 3 — Temporal Consistency & Observability

#### 3a — Temporal Validation Service (warn-only)

Unchanged from original plan. **No auto-mutation, ever, in Phase 3.**

#### 3b — Temporal Query Methods

**Correction**: The existing `idx_facts_temporal` is btree, not GiST. It supports prefix lookups on `(user_id, valid_from)` with filter on `valid_to` — good for "facts valid at a point in time for a user." It does NOT support `&&` range overlap.

Design the temporal queries as **btree-optimized** queries, not GiST-overlap queries:

```python
async def get_facts_at_time(
    self, project_id: UUID, timestamp: datetime
) -> list[Fact]:
    """Return facts valid at a specific point in time.

    Uses btree index on (user_id, valid_from, valid_to):
      - valid_from <= timestamp  →  btree prefix can seek
      - (valid_to IS NULL OR valid_to > timestamp)  →  residual filter
    """

async def get_facts_in_range(
    self, project_id: UUID, start: datetime, end: datetime
) -> list[Fact]:
    """Return facts whose valid range overlaps [start, end).

    Btree-backed query (NOT GiST &&):
      - valid_from < end AND (valid_to IS NULL OR valid_to > start)
    """
```

Add a **separate GiST index** for the `&&` overlap operator only if profiling shows the btree plan is too slow:

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_facts_temporal_gist
ON facts USING gist (tstzrange(valid_from, valid_to, '[)'))
WHERE invalid_at IS NULL;
```

But **this is not in Phase 3 scope** — mark it as a post-launch tuning step. Ship the btree queries first, profile with `EXPLAIN ANALYZE` against production-scale data, add GiST only if needed.

#### 3c — Temporal Conflict Metrics

Unchanged from original plan. Add structured counters at every conflict point.

#### 3d — Reserve `ENRICHMENT_OBSERVATIONS` Bit 6

Unchanged. Add the constant and the placeholder file.

---

### Execution Order (updated)

```
Phase 1  ───────────────►  (ship standalone)

Phase 2a (migration) ────►  (prerequisite for 2b, but can run after 1)

Phase 2b (repo) ─────────►  (no batch_upsert, just on_conflict param)

Phase 2d (tests) ────────►

Phase 3a (validation) ───►  (can parallel with 2b/d)

Phase 3b (queries) ──────►  (btree-backed, GiST flagged as tuning)

Phase 3c (metrics) ──────►  (can parallel with 3a/b)

Phase 3d (bit 6) ────────►  (any time)
```

### File Change Summary (final)

| File | Phase | What |
|---|---|---|
| `packages/graph_backend/postgres.py` | 1 | `valid_to = CASE ... END` fix |
| `migrations/versions/0024_add_temporal_exclusion_facts.py` | 2a | New migration |
| `repositories/fact_repository.py` | 2b | `on_conflict` param on `batch_create()` |
| `services/temporal_service.py` | 3a | New file (warn-only validator) |
| `services/graph_service.py` | 3b | Temporal query methods (btree-backed) |
| `workers/tasks/base.py` | 3d | `ENRICHMENT_OBSERVATIONS = 1 << 6` |
| `workers/tasks/compute_observations.py` | 3d | Placeholder |
| `tests/integration/test_temporal_conflicts.py` | 1+2d | ~18 test cases |
| `tests/unit/test_temporal_service.py` | 3a | Validator tests |
| `tests/unit/test_enrichment_bitmask.py` | 3d | Verify bit 6 is unique |

### What's NOT in the plan (by design)

| Excluded | Reason |
|---|---|
| `batch_upsert()` for facts | No concrete caller — would be dead code |
| GiST range index on facts | Add only if profiling proves btree plan is too slow |
| Auto-expiry of superseded ranges | Behind feature flag, only after Phase 3a warn-only data justifies it |
| Observations worker implementation | Bit 6 reserved, implementation deferred per your direction |
| Re-index `idx_facts_temporal` model name | Papercut — file an issue to rename to `idx_facts_temporal` to match migration |

Shall I start implementing Phase 1?
