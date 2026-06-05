# Cross-Tenant Isolation Test Matrix

| | |
|---|---|
| **Document** | 14-testing/04-cross-tenant-test-matrix.md |
| **Phase** | 5 — Hardening |
| **Author** | Technical Writing |
| **Status** | Draft |

---

## 1. Objective

Verify that **no data leaks between tenants** under any access pattern. This is OpenZep's most critical security property: a multi-tenant system where Tenant A can read Tenant B's data is a production-zero defect.

The test matrix exhaustively covers:
- **3 tenants** (A, B, C)
- **6 resource types** (users, sessions, facts, episodes, graph nodes, search)
- **3 access methods** (direct ID, enumeration, search-based)
- **= 54 combinations** (3 × 6 × 3), each tested bidirectionally with assertions

---

## 2. Methodology

### 2.1 Principle

Each test:
1. **Seeds** independent data for Tenant A and Tenant B
2. **Authenticates** as Tenant A
3. **Attempts** to access Tenant B's resource
4. **Asserts** the response contains no Tenant B data

Tests are **symmetric**: the reverse direction (B trying to access A) is a separate test.

### 2.2 HttpResponse Semantics

| Scenario | HTTP Status | Rationale |
|---|---|---|
| Tenant A accesses own resource | 200 | Normal operation |
| Tenant A accesses Tenant B's direct resource by ID | **404** | Hide existence of other tenants' resources |
| Tenant A lists resources | 200 | Returns only Tenant A's resources |
| Tenant A searches | 200 | Returns only Tenant A's results |
| Tenant A accesses `/admin` without super_key scope | **403** | Admin scope is explicit, not hidden |

**Why 404 and not 403?**

Returning 404 for cross-tenant access prevents resource enumeration. If Tenant A got 403 when Tenant B's resource exists and 404 when it doesn't, they could infer which resource IDs belong to other tenants. A uniform 404 for both cases (resource doesn't exist OR resource belongs to another tenant) eliminates this oracle.

The sole exception is the admin panel: a non-admin key attempting to access `/admin` endpoints returns 403 because the admin scope is an explicit authorization boundary, not a data access boundary.

---

## 3. Resource Types and Access Methods

### 3.1 Resource Types

| Resource | Endpoint Pattern | Seed Data |
|---|---|---|
| **users** | `GET /v1/users/{user_id}` | Create user with unique `external_id` + name |
| **sessions** | `GET /v1/users/{owner_id}/sessions/{session_id}` | Create session with unique `external_id` |
| **facts** | `GET /v1/users/{owner_id}/facts/{fact_id}` | Insert fact with unique content string |
| **episodes** | `GET /v1/users/{owner_id}/sessions/{session_id}/messages` | Insert episode with unique content per tenant |
| **graph nodes** | `GET /v1/users/{owner_id}/graph/nodes/{node_id}` | Create entity node via Graphiti with unique name |
| **search** | `GET /v1/users/{owner_id}/search?query={term}` | Insert tenant-specific content that matches a known search term |

### 3.2 Access Methods

#### Direct ID

The attacker knows (or guesses) the UUID of a resource belonging to another tenant.

```
Tenant A → GET /v1/users/{tenant_b_user_id}
        → Expect 404, or 200 with zero Tenant B data
```

**Test setup:**
1. Seed Tenant B with a resource → get its UUID
2. Authenticate as Tenant A
3. Request `/{resource_type}/{tenant_b_resource_uuid}`
4. Assert 404 OR assert returned data belongs to Tenant A

#### Enumeration (List)

The attacker lists resources and checks that no other tenant's data appears.

```
Tenant A → GET /v1/users
        → Expect only users belonging to Tenant A
```

**Test setup:**
1. Seed Tenant A with 3 resources, Tenant B with 3 resources
2. Authenticate as Tenant A
3. Request the list endpoint
4. Assert response `data` array contains exactly 3 items, all belonging to Tenant A

#### Search

The attacker searches for content that only exists in another tenant's data.

```
Tenant A → GET /v1/users/{some_user_id}/search?query="known_tenant_b_phrase"
        → Expect no Tenant B results
```

**Test setup:**
1. Seed Tenant B with content containing distinctive phrase "purple-monkey-dishwasher"
2. Authenticate as Tenant A
3. Search for "purple-monkey-dishwasher"
4. Assert zero results

---

## 4. Test Matrix

### 4.1 Full Combination Matrix

```
Tenants:          A, B, C  (3)
Resources:        users, sessions, facts, episodes, graph_nodes, search  (6)
Access methods:   direct_id, enumeration, search  (3)
Directions:       attacker→victim (pairs A→B, A→C, B→A, B→C, C→A, C→B)  (6)

Total tests:      3 × 6 × 3 × 6 = 324  (covering all pairs)
```

### 4.2 Matrix Table (Abbreviated, showing Tenant A → Tenant B)

| # | Attacker | Resource | Access Method | Seed Victim Data | Test Action | Expected Result |
|---|---|---|---|---|---|---|
| 1 | A | users | direct_id | B creates user `u_b1` | A gets `u_b1` | 404 |
| 2 | A | users | enumeration | B creates 3 users | A lists users | Only A's users returned |
| 3 | A | sessions | direct_id | B creates session `s_b1` on B's user | A gets `s_b1` | 404 |
| 4 | A | sessions | enumeration | B creates 3 sessions | A lists sessions | Only A's sessions returned |
| 5 | A | facts | direct_id | B creates fact `f_b1` | A gets `f_b1` | 404 |
| 6 | A | facts | enumeration | B creates 3 facts | A lists facts | Only A's facts returned |
| 7 | A | episodes | direct_id | B creates episode `e_b1` | A gets `e_b1` | 404 |
| 8 | A | episodes | enumeration | B creates 3 episodes in a session | A lists messages | Only A's messages returned |
| 9 | A | graph_nodes | direct_id | B creates node `n_b1` | A gets `n_b1` | 404 |
| 10 | A | graph_nodes | enumeration | B creates 3 nodes | A lists nodes | Only A's nodes returned |
| 11 | A | search | search | B has content "purple-monkey" | A searches for it | No B results returned |

### 4.3 Negative Cases (Should NOT Leak)

These edge cases must also be tested:

| # | Scenario | Expected |
|---|---|---|
| 12 | A accesses B's resource via ID that happens to match A's resource ID pattern | 404 (never redirect or conflate) |
| 13 | A POSTs B's user_id in the URL path (path traversal) | 404 |
| 14 | A uses B's API key on A's endpoint | 401 (key doesn't exist for A) |
| 15 | A sends request with no API key (public endpoint) | Only public endpoints accessible |
| 16 | A's expired API key returns 401 | No data leaked |
| 17 | A attempts to access admin endpoints without super_key | 403 |

---

## 5. Test Implementation

### 5.1 Parametrized Test Structure

```python
# tests/security/test_cross_tenant.py
import uuid
import pytest
from httpx import AsyncClient

# Tenant configuration
TENANTS = {
    "A": {"org_id": uuid.UUID("00000000-0000-0000-0000-000000000001"), "api_key": "mg_test_a"},
    "B": {"org_id": uuid.UUID("00000000-0000-0000-0000-000000000002"), "api_key": "mg_test_b"},
    "C": {"org_id": uuid.UUID("00000000-0000-0000-0000-000000000003"), "api_key": "mg_test_c"},
}

RESOURCE_TYPES = ["users", "sessions", "facts", "episodes", "graph_nodes", "search"]
ACCESS_METHODS = ["direct_id", "enumeration", "search"]

# Generate all combinations
@pytest.mark.parametrize("attacker", ["A", "B", "C"])
@pytest.mark.parametrize("victim", ["A", "B", "C"])
@pytest.mark.parametrize("resource_type", RESOURCE_TYPES)
@pytest.mark.parametrize("access_method", ACCESS_METHODS)
async def test_cross_tenant_isolation(
    attacker: str,
    victim: str,
    resource_type: str,
    access_method: str,
    seed_tenant_data,
    tenant_client_factory,
):
    # Skip trivial case: same tenant is not cross-tenant
    if attacker == victim:
        pytest.skip("Same tenant — not a cross-tenant test")

    # Create authenticated client for attacker
    client = tenant_client_factory(TENANTS[attacker]["api_key"])

    # Execute the access attempt
    response = await execute_access(
        client, victim, resource_type, access_method, TENANTS
    )

    # Assert no data from victim leaked
    assert_no_cross_tenant_data(response, victim, resource_type)
```

### 5.2 Fixtures

```python
# tests/security/conftest.py
import uuid
import pytest
from httpx import ASGITransport, AsyncClient

@pytest.fixture(scope="module")
async def seed_tenant_data(db_session, tenant_client_factory):
    """Pre-seed each tenant with independent test data.

    This runs once per module and creates distinct resources per tenant.
    """
    for tenant_id, config in TENANTS.items():
        client = tenant_client_factory(config["api_key"])

        # Create user
        user_resp = await client.post("/v1/users", json={
            "user_id": f"user_{tenant_id}",
            "name": f"User {tenant_id}",
        })
        user_id = user_resp.json()["id"]

        # Create session
        session_resp = await client.post(
            f"/v1/users/{user_id}/sessions",
            json={"session_id": f"sess_{tenant_id}"},
        )
        session_id = session_resp.json()["id"]

        # Add memory (which creates episodes, facts, graph nodes)
        await client.post(f"/v1/users/{user_id}/memory", json={
            "messages": [
                {"role": "user", "content": f"Hello from tenant {tenant_id}. purple-monkey-dishwasher-{tenant_id}."},
                {"role": "assistant", "content": f"Hello tenant {tenant_id}!"},
            ]
        })

        # Store seeded IDs for test access
        yield {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "session_id": session_id,
            "distinctive_content": f"purple-monkey-dishwasher-{tenant_id}",
        }


@pytest.fixture
def tenant_client_factory(app):
    """Factory fixture — returns a function that creates an authenticated client for any API key."""
    def _create(api_key: str) -> AsyncClient:
        transport = ASGITransport(app=app)
        client = AsyncClient(transport=transport, base_url="http://test")
        client.headers["Authorization"] = f"Bearer {api_key}"
        return client
    return _create


def assert_no_cross_tenant_data(response, victim_tenant: str, resource_type: str):
    """Assert that the response contains no data from the victim tenant."""
    victim_prefix = f"_{victim_tenant}"  # e.g. "_B" in "user_B", "purple-monkey-dishwasher-B"

    if resource_type == "search":
        data = response.json().get("results", [])
    else:
        data = response.json().get("data", [response.json()])

    if isinstance(data, list):
        for item in data:
            item_str = str(item)
            assert victim_prefix not in item_str, (
                f"Cross-tenant leak detected! Victim {victim_tenant} data "
                f"found in {resource_type} response from attacker."
            )
    elif isinstance(data, dict):
        item_str = str(data)
        assert victim_prefix not in item_str, (
            f"Cross-tenant leak detected! Victim {victim_tenant} data "
            f"found in {resource_type} response from attacker."
        )
```

### 5.3 Access Execution Function

```python
async def execute_access(
    client: AsyncClient,
    victim: str,
    resource_type: str,
    access_method: str,
    tenants: dict,
) -> httpx.Response:
    """Execute a single cross-tenant access attempt."""

    victim_config = tenants[victim]

    if access_method == "direct_id":
        # Victim's user ID is known from seed data
        # Access it directly
        victim_user_id = f"user_{victim}"  # or UUID from seed

        if resource_type == "users":
            return await client.get(f"/v1/users/{victim_user_id}")

        elif resource_type == "sessions":
            victim_session_id = f"sess_{victim}"
            return await client.get(
                f"/v1/users/{victim_user_id}/sessions/{victim_session_id}"
            )

        elif resource_type == "facts":
            # List victim's facts first to get a fact ID, then try direct access
            list_resp = await client.get(f"/v1/users/{victim_user_id}/facts")
            facts = list_resp.json().get("data", [])
            if not facts:
                return list_resp  # No facts to test, but response should be empty
            fact_id = facts[0]["id"]
            return await client.get(f"/v1/users/{victim_user_id}/facts/{fact_id}")

        # ... similar for episodes, graph_nodes

        # For search, "direct_id" means searching with victim's distinctive content
        elif resource_type == "search":
            return await client.get(
                f"/v1/users/{victim_user_id}/search",
                params={"query": f"purple-monkey-dishwasher-{victim}"},
            )

    elif access_method == "enumeration":
        # List resources and verify victim data is absent
        if resource_type == "users":
            return await client.get("/v1/users")

        elif resource_type == "sessions":
            victim_user_id = f"user_{victim}"
            return await client.get(f"/v1/users/{victim_user_id}/sessions")

        # ... etc

    elif access_method == "search":
        if resource_type == "search":
            # Search across all resources for victim's content
            victim_user_id = f"user_{victim}"
            return await client.get(
                f"/v1/users/{victim_user_id}/search",
                params={"query": f"purple-monkey-dishwasher-{victim}"},
            )
        # Other resource types don't support search access method
        # (sessions, facts, etc. are accessed by ID or list, not search)
        return httpx.Response(400)

    raise ValueError(f"Unknown combination: {resource_type} / {access_method}")
```

---

## 6. Performance Target

| Metric | Target |
|---|---|
| Total tests | 324 (all pairs + edge cases) |
| Execution time (full suite) | < 30 seconds |
| Seed data setup | < 10 seconds (reused across tests) |
| Per-test latency | < 100 ms |

**Optimization strategies:**
- Seed all tenants' data once per module (scope `module` or `session`) and reuse across tests
- Use `pytest-xdist` only for unit tests — these security tests must run **sequentially** to avoid DB state conflicts
- SQLAlchemy `flush()` (not `commit()`) after seeding, rollback at module end

```bash
pytest tests/security/ -v --timeout=60 --durations=10
```

---

## 7. CI Integration

| Gate | Action |
|---|---|
| Every PR | Cross-tenant matrix runs as part of `test-security` stage |
| Merge to `main` | Must pass all 324 tests |
| Failure | Blocks merge. If a false positive, requires tech lead to whitelist and file a bug |
| Frequency | Every commit (fast enough at < 30s to run per-commit) |

---

## 8. Enforcing Isolation at the Query Layer

The test matrix validates that isolation **works**. The actual enforcement mechanism is:

1. **Every repository method** appends `organization_id == <current_org>` to every query
2. **The auth middleware** extracts `organization_id` from the API key and stores it in request state
3. **All list endpoints** filter by the authenticated organization
4. **Graph queries** use namespaced graph keys (e.g., `org:{org_id}:entity:*` in FalkorDB)

This pattern is tested at the **unit level** (repository tests assert SQL contains the org filter) and **integration level** (the cross-tenant matrix).

```python
# Example repository guard pattern:
class UserRepository:
    async def get_by_id(self, user_id: UUID, organization_id: UUID) -> User | None:
        result = await self._db.execute(
            select(User).where(
                User.id == user_id,
                User.organization_id == organization_id,  # ← always present
            )
        )
        return result.scalar_one_or_none()
```

Any repository method missing the `organization_id` filter is a security bug and will be caught by the cross-tenant test matrix.

---

## 9. False Positive Handling

| Scenario | Resolution |
|---|---|
| Test fails because victim's seed data wasn't created in time | Increase seed timeout; verify seed ordering |
| Test fails because resource type requires specific user/session relationships | Use a modular seed that creates all required parent resources |
| Test fails intermittently (rate limiting throttle) | Disable rate limiting in test config; test rate limiting separately |
| Test fails because search returns cross-tenant results from shared index | Verify that `organization_id` filter is applied to search queries at the SQL level |
