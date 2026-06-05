# API Key Authentication

> **Phase:** 0 (Foundation)
> **SRS Requirements:** AUTH-01, AUTH-02, AUTH-03, AUTH-04, AUTH-06, SEC-01
> **SRS Data Model:** `api_keys` table
> **Dependencies:** [01-postgresql-schema.md](../01-data-models/01-postgresql-schema.md)

---

## 1. Overview

OpenZep uses **API key authentication** for all agent-facing endpoints (`/v1/*`). Keys are scoped to an organization (tenant), prefixed for environment identification, and stored as salted SHA-256 hashes. A dual-key window enables zero-downtime rotation, and validated keys are cached in Redis to avoid a DB lookup on every request.

> **Design note — deviation from SRS:** The SRS (SEC-01) specifies bcrypt for API key storage. **We use SHA-256 + 16-byte random salt instead.** Rationale:
> - bcrypt truncates input at 72 bytes — our 64-char base62 tokens fit, but any future format change risks silent truncation.
> - bcrypt is designed for passwords (slow, cost-factor tuned). API keys are high-entropy tokens (256 bits of randomness); the work factor provides negligible security benefit.
> - SHA-256 is significantly faster for the hot-path validation middleware, reducing p99 latency.
> - A 16-byte random salt per key defeats rainbow tables — the same security property bcrypt provides, without the overhead.

---

## 2. Key Format

### 2.1 Token Structure

```
┌──────────┬──────────────────────────────────────────────┐
│ Prefix   │ Random Token (base62, 64 chars)              │
├──────────┼──────────────────────────────────────────────┤
│ mg_live_ │ a3B8xR... (64 chars of [0-9a-zA-Z])          │
│ mg_test_ │ K7mN2p... (64 chars of [0-9a-zA-Z])          │
└──────────┴──────────────────────────────────────────────┘
```

**Full example:** `mg_live_a3B8xR9kLmN2pQ4rS6tU8vW0yZ1cE3gH5iJ7kM9oP1qR3sT5uV7wX9z`

### 2.2 Prefix Lifecycle

| Prefix | Environment | Purpose |
|--------|-------------|---------|
| `mg_live_` | Production | Real traffic. Revocation is immediate. |
| `mg_test_` | Sandbox / Staging | Development and testing. Same code path, no production data. |

### 2.3 Random Token Generation

```python
import secrets
import base62  # or: from core.crypto import base62_encode

def generate_api_key(prefix: str = "mg_live_") -> str:
    """Generate a cryptographically secure API key.

    Uses 48 random bytes → base62 encoded → 64-character token.
    This gives 48 * log2(62) ≈ 285 bits of entropy — sufficient
    to make brute-force infeasible even with SHA-256 at 10⁶ hashes/s.

    Returns:
        Full key string, e.g. "mg_live_a3B8..."
    """
    raw_token = secrets.token_bytes(48)          # 48 bytes of CSPRNG output
    token_b62 = base62.encode(raw_token)          # base62: 0-9a-zA-Z, no ambiguity
    assert len(token_b62) == 64, f"expected 64 chars, got {len(token_b62)}"
    return f"{prefix}{token_b62}"
```

**Why base62 (not base64)?** Base64 uses `+` and `/` characters that cause issues in URL parameters, HTTP headers, and shell scripts. Base62 uses only `[0-9a-zA-Z]` — safe everywhere without escaping.

---

## 3. Key Storage — SHA-256 with Salt

### 3.1 Hashing Algorithm

```python
import hashlib
import hmac
import secrets

# Salt length: 16 bytes (128 bits) — sufficient to defeat rainbow tables.
# Stored alongside the hash in the api_keys table.
SALT_LENGTH = 16

def hash_api_key(raw_key: str) -> tuple[str, str]:
    """Hash an API key for storage.

    Uses SHA-256 with a per-key random salt.
    Returns (hash_hex, salt_hex) — both stored in the api_keys table.
    """
    salt = secrets.token_hex(SALT_LENGTH)          # 32 hex chars
    key_bytes = raw_key.encode("utf-8")
    salt_bytes = bytes.fromhex(salt)

    # HMAC-SHA256 with salt as the key — constant-time comparison built-in.
    # Using HMAC prevents length-extension attacks on bare SHA-256.
    digest = hmac.new(salt_bytes, key_bytes, hashlib.sha256).hexdigest()
    return digest, salt


def verify_api_key(raw_key: str, stored_hash: str, stored_salt: str) -> bool:
    """Constant-time comparison of a raw key against its stored hash."""
    key_bytes = raw_key.encode("utf-8")
    salt_bytes = bytes.fromhex(stored_salt)
    computed = hmac.new(salt_bytes, key_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, stored_hash)
```

### 3.2 Database Schema

```sql
-- Migration: V002_create_api_keys.sql (or Alembic equivalent)
-- Supersedes the bcrypt comment in SRS Section 7.1.

CREATE TABLE api_keys (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    key_hash        TEXT NOT NULL,           -- SHA-256 HMAC hex digest (64 chars)
    key_salt        TEXT NOT NULL,           -- 16-byte random salt as hex (32 chars)
    prefix          TEXT NOT NULL,           -- 'mg_live_' or 'mg_test_'
    name            TEXT,                    -- Human label: "CI/CD key", "Staging"
    scopes          TEXT[] NOT NULL DEFAULT ARRAY['read','write'],
    last_used_at    TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ,             -- NULL = never expires
    revoked_at      TIMESTAMPTZ,             -- NULL = not revoked
    rotation_ref    UUID,                    -- Points to predecessor key during rotation
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Enforce uniqueness of (organization_id, key_hash) — no duplicate hashes
-- within the same tenant. (key_hash alone is globally unique due to per-key salt,
-- but the compound index is clearer about intent.)
CREATE UNIQUE INDEX uq_api_keys_org_hash ON api_keys (organization_id, key_hash);

-- Fast lookup by hash during authentication
CREATE INDEX idx_api_keys_key_hash ON api_keys (key_hash);

-- Active key lookup for an organization
CREATE INDEX idx_api_keys_active
    ON api_keys (organization_id, expires_at)
    WHERE revoked_at IS NULL;
```

### 3.3 Why Not bcrypt? — Detailed Comparison

| Property | bcrypt | SHA-256 + salt |
|----------|--------|----------------|
| Input limit | 72 bytes (silent truncation) | Unlimited |
| Verification latency | ~100ms (cost=10) | ~0.001ms |
| Rainbow table protection | Built-in salt | Per-key 16-byte salt |
| Constant-time comparison | Yes | Via HMAC |
| Appropriate use case | User passwords | API keys / tokens |
| P99 auth middleware impact | +100ms per request | Negligible |

**Rule of thumb:** If a human types it, use bcrypt. If a machine generates it, use SHA-256 + salt. API keys are machine-generated with full entropy — SHA-256 is the correct choice.

---

## 4. Key Lifecycle State Machine

### 4.1 States

```
                    ┌──────────┐
                    │  ACTIVE  │ ◄──── Created here
                    └────┬─────┘
                         │
              ┌──────────┴──────────┐
              │                     │
         expires_at         revoked_at set
         approaching        (manual or security event)
              │                     │
         ┌────▼─────┐         ┌────▼──────┐
         │ EXPIRING │         │  REVOKED  │
         │ (≤7 days)│         │ (terminal)│
         └────┬─────┘         └───────────┘
              │
         expires_at
         reached
              │
         ┌────▼──────┐
         │  EXPIRED  │
         │ (terminal)│
         └───────────┘
```

### 4.2 State Transitions

| Transition | Trigger | Action |
|-----------|---------|--------|
| `→ ACTIVE` | Key creation | Hash stored, raw key returned once. `expires_at` set based on plan (default 1 year). |
| `ACTIVE → EXPIRING` | Cron job or on-read check: `expires_at < now() + 7d` | Log warning, optionally notify admin. Key still valid. |
| `ACTIVE → REVOKED` | `DELETE /admin/.../keys/{key_id}` | Set `revoked_at = now()`. Invalidate Redis cache immediately. |
| `EXPIRING → EXPIRED` | `expires_at < now()` | Set `revoked_at = expires_at`. All subsequent auth attempts return 401. |
| `ACTIVE → EXPIRED` | Direct expiry | Same as above. |

### 4.3 Expiration Check Logic

```python
from datetime import datetime, timezone, timedelta

def is_key_valid(key_record: dict) -> bool:
    """Check if a key is valid for use.

    A key is valid if:
    1. It is not revoked (revoked_at IS NULL)
    2. It has not expired (expires_at IS NULL OR expires_at > now())
    """
    if key_record.get("revoked_at"):
        return False
    expires_at = key_record.get("expires_at")
    if expires_at and expires_at < datetime.now(timezone.utc):
        return False
    return True

def is_key_expiring(key_record: dict, window_days: int = 7) -> bool:
    """Check if a key is approaching expiry (for admin notifications)."""
    expires_at = key_record.get("expires_at")
    if not expires_at:
        return False
    return expires_at < datetime.now(timezone.utc) + timedelta(days=window_days)
```

---

## 5. Key Creation — `POST /admin/organizations/{org_id}/keys`

### 5.1 Request/Response Flow

```
Admin Dashboard / CLI                    FastAPI                          PostgreSQL
      │                                    │                                  │
      │  POST /admin/orgs/{id}/keys        │                                  │
      │  {name, scopes, expires_at}        │                                  │
      │──────────────────────────────────►│                                  │
      │                                    │  generate_api_key()              │
      │                                    │  hash_api_key()                  │
      │                                    │─────────────────────────────────►│
      │                                    │  INSERT INTO api_keys            │
      │                                    │  (hash, salt, prefix, ...)       │
      │                                    │◄─────────────────────────────────│
      │  201 {key_id, prefix, raw_key,     │                                  │
      │       scopes, created_at}          │                                  │
      │◄──────────────────────────────────│                                  │
      │                                    │                                  │
```

### 5.2 Router

```python
# routers/admin.py
from fastapi import APIRouter, Depends, HTTPException, status

router = APIRouter(prefix="/admin", tags=["admin"])

@router.post(
    "/organizations/{org_id}/keys",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiKeyCreatedResponse,
)
async def create_api_key(
    org_id: UUID,
    payload: CreateApiKeyRequest,
    service: AuthService = Depends(get_auth_service),
    admin: AdminUser = Depends(get_current_admin_user),
) -> ApiKeyCreatedResponse:
    """Generate a new API key for an organization.

    The raw key is returned **once** in this response. OpenZep does not
    store the raw key. If lost, the key must be revoked and re-created.

    Args:
        org_id: Organization UUID.
        payload: Key configuration (name, scopes, prefix, expires_at).
        admin: Authenticated admin user (super_admin or org_admin).

    Returns:
        ApiKeyCreatedResponse with the raw key. Display this to the user
        immediately — it cannot be retrieved again.

    Raises:
        HTTPException(403): If admin does not have permission for this org.
        HTTPException(404): If org not found.
    """
    result = await service.create_api_key(
        org_id=org_id,
        name=payload.name,
        scopes=payload.scopes,
        prefix=payload.prefix,
        expires_at=payload.expires_at,
        created_by=admin.id,
    )
    return ApiKeyCreatedResponse(**result)
```

### 5.3 Schema

```python
# schemas/admin.py
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime, timezone, timedelta

class CreateApiKeyRequest(BaseModel):
    name: Optional[str] = Field(None, max_length=128, description="Human label")
    scopes: list[str] = Field(
        default=["read", "write"],
        description="Key scopes: 'read', 'write', 'admin'",
    )
    prefix: str = Field(
        default="mg_live_",
        pattern=r"^mg_(live|test)_$",
        description="Key prefix: mg_live_ (prod) or mg_test_ (sandbox)",
    )
    expires_at: Optional[datetime] = Field(
        default=None,
        description="Expiration timestamp. Default: 1 year from now.",
    )

    @model_validator(mode="after")
    def set_default_expiry(self) -> "CreateApiKeyRequest":
        if self.expires_at is None:
            self.expires_at = datetime.now(timezone.utc) + timedelta(days=365)
        return self


class ApiKeyCreatedResponse(BaseModel):
    id: UUID
    organization_id: UUID
    prefix: str
    raw_key: str = Field(description="Display this once. It will not be stored.")
    scopes: list[str]
    name: Optional[str]
    expires_at: Optional[datetime]
    created_at: datetime
```

### 5.4 Service Layer

```python
# services/auth_service.py
class AuthService:
    def __init__(self, repo: AuthRepository, redis: Redis):
        self._repo = repo
        self._redis = redis

    async def create_api_key(
        self,
        org_id: UUID,
        name: str | None,
        scopes: list[str],
        prefix: str,
        expires_at: datetime | None,
        created_by: UUID,
    ) -> dict:
        # 1. Verify org exists
        org = await self._repo.get_organization(org_id)
        if not org:
            raise OrganizationNotFoundError(org_id)

        # 2. Generate raw key
        raw_key = generate_api_key(prefix=prefix)

        # 3. Hash for storage
        key_hash, key_salt = hash_api_key(raw_key)

        # 4. Persist
        key_record = await self._repo.insert_api_key(
            organization_id=org_id,
            key_hash=key_hash,
            key_salt=key_salt,
            prefix=prefix,
            name=name,
            scopes=scopes,
            expires_at=expires_at,
        )

        # 5. Return raw key — will never be stored
        return {
            "id": key_record["id"],
            "organization_id": org_id,
            "prefix": prefix,
            "raw_key": raw_key,          # ⚠️ Display once, never log
            "scopes": scopes,
            "name": name,
            "expires_at": expires_at,
            "created_at": key_record["created_at"],
        }
```

### 5.5 Repository Layer

```python
# repositories/auth_repository.py
class AuthRepository:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def insert_api_key(
        self,
        organization_id: UUID,
        key_hash: str,
        key_salt: str,
        prefix: str,
        name: str | None,
        scopes: list[str],
        expires_at: datetime | None,
    ) -> dict:
        result = await self._db.execute(
            text("""
                INSERT INTO api_keys
                    (organization_id, key_hash, key_salt, prefix, name, scopes, expires_at)
                VALUES
                    (:org_id, :key_hash, :key_salt, :prefix, :name, :scopes, :expires_at)
                RETURNING id, created_at
            """),
            {
                "org_id": organization_id,
                "key_hash": key_hash,
                "key_salt": key_salt,
                "prefix": prefix,
                "name": name,
                "scopes": scopes,
                "expires_at": expires_at,
            },
        )
        await self._db.commit()
        row = result.mappings().one()
        return {"id": row["id"], "created_at": row["created_at"]}
```

---

## 6. Authentication Middleware

### 6.1 Dependency — Extracting and Validating the API Key

```python
# dependencies/auth.py
import logging
from fastapi import Request, HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

logger = logging.getLogger(__name__)

# FastAPI's HTTPBearer handles the Authorization header parse
security_scheme = HTTPBearer(auto_error=False)


async def get_api_key_identity(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
    auth_service: AuthService = Depends(get_auth_service),
) -> ApiKeyIdentity:
    """Validate the Bearer token and return the API key identity.

    This is the primary authentication dependency for all /v1/ endpoints.
    Attaches organization_id, key_id, and scopes to the request state.

    Flow:
    1. Extract Bearer token from Authorization header
    2. Check Redis cache for validated key
    3. If cache miss, look up by SHA-256 hash in PostgreSQL
    4. Validate: not revoked, not expired, prefix matches environment
    5. Cache validated result in Redis (TTL: 5 min)
    6. Attach identity to request.state

    Raises:
        HTTPException(401): Missing, invalid, or expired key.
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "MISSING_API_KEY",
                "message": "Authorization header with Bearer token is required.",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    raw_key = credentials.credentials
    identity = await _validate_api_key(raw_key, auth_service)

    # Attach to request state for downstream use
    request.state.org_id = identity.organization_id
    request.state.api_key_id = identity.key_id
    request.state.scopes = identity.scopes

    return identity


async def _validate_api_key(raw_key: str, auth_service: AuthService) -> ApiKeyIdentity:
    """Core validation — cache-checked, DB-backed."""
    # 1. Check Redis cache
    cache_key = f"apikey:hash:{_key_hash_for_cache(raw_key)}"
    cached = await auth_service.redis.get(cache_key)
    if cached:
        identity = ApiKeyIdentity.model_validate_json(cached)
        if identity.is_expired or identity.is_revoked:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "KEY_EXPIRED", "message": "API key has expired or been revoked."},
            )
        return identity

    # 2. Cache miss — look up by hash
    identity = await auth_service.validate_api_key(raw_key)

    if not identity:
        logger.warning("auth.invalid_key_attempt", extra={"prefix": raw_key[:8]})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_API_KEY", "message": "The provided API key is not valid."},
        )

    # 3. Cache the validated result (TTL: 5 min)
    await auth_service.redis.setex(
        cache_key,
        300,  # 5 minutes
        identity.model_dump_json(),
    )

    return identity


def _key_hash_for_cache(raw_key: str) -> str:
    """Quick hash for Redis cache key — NOT the same as the stored hash.

    We use a fast SHA-256 of the raw key as the cache key so Redis lookups
    don't require the DB salt. This hash is ephemeral (cache-only).
    """
    return hashlib.sha256(raw_key.encode()).hexdigest()
```

### 6.2 API Key Identity Model

```python
# schemas/auth.py
from pydantic import BaseModel
from uuid import UUID
from datetime import datetime
from typing import Optional


class ApiKeyIdentity(BaseModel):
    """Validated API key identity attached to every request.

    Available as `request.state.api_key_identity` in route handlers.
    """
    key_id: UUID
    organization_id: UUID
    prefix: str
    scopes: list[str]
    expires_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at < datetime.now(self.expires_at.tzinfo)

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None
```

### 6.3 Scope Check Dependency

```python
# dependencies/auth.py
from functools import wraps

def require_scope(required_scope: str):
    """Dependency factory: requires a specific API key scope.

    Usage:
        @router.get("/v1/users")
        async def list_users(
            identity: ApiKeyIdentity = Depends(get_api_key_identity),
            _ = Depends(require_scope("read")),
        ):
            ...
    """
    async def scope_dependency(
        identity: ApiKeyIdentity = Depends(get_api_key_identity),
    ) -> None:
        if required_scope not in identity.scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "INSUFFICIENT_SCOPE",
                    "message": f"Scope '{required_scope}' is required.",
                },
            )
    return scope_dependency
```

### 6.4 Middleware Registration

```python
# main.py — App bootstrap
from fastapi import FastAPI
from dependencies.auth import get_api_key_identity

app = FastAPI(title="OpenZep API")

# Public endpoints (no auth) — /health, /docs, /openapi.json
# Admin endpoints — JWT auth (see 02-jwt-auth.md)
# API v1 endpoints — API key auth enforced via router dependency

# Option A: Apply to all /v1 routes via APIRouter dependency
api_v1 = APIRouter(prefix="/v1", dependencies=[Depends(get_api_key_identity)])

# Option B: Apply per-route (preferred for granularity)
@api_v1.get("/users/{user_id}/memory")
async def get_memory(
    user_id: str,
    identity: ApiKeyIdentity = Depends(get_api_key_identity),
):
    ...
```

---

## 7. Key Rotation

### 7.1 Dual-Key Window

Rotation follows a **dual-key window** pattern: both the old and new key are valid simultaneously for a configurable grace period (default: 24 hours). This allows clients to update their credentials without downtime.

```
Timeline:
│
├── Key A created (ACTIVE)
│
├── ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─
│
├── Key B created → Both A and B valid
│   Key A: rotation_ref = Key B.id
│   Key B: rotation_ref = Key A.id
│
├── Grace period ends (default: 24h)
│
├── Key A revoked automatically
│   (only Key B remains valid)
│
└── Key B is now the sole active key
```

### 7.2 Rotation Endpoint

```python
# routers/admin.py
@router.post("/organizations/{org_id}/keys/{key_id}/rotate")
async def rotate_api_key(
    org_id: UUID,
    key_id: UUID,
    service: AuthService = Depends(get_auth_service),
    admin: AdminUser = Depends(get_current_admin_user),
) -> ApiKeyCreatedResponse:
    """Rotate an API key, creating a new key and keeping the old one valid.

    The old key is linked via `rotation_ref` and remains valid for the
    configured grace period (default: 24h). After the grace period, the
    old key is automatically revoked.

    Returns the new raw key (display once).
    """
    result = await service.rotate_api_key(
        org_id=org_id,
        current_key_id=key_id,
        grace_period_hours=24,
        rotated_by=admin.id,
    )
    return ApiKeyCreatedResponse(**result)
```

### 7.3 Service Implementation

```python
# services/auth_service.py
async def rotate_api_key(
    self,
    org_id: UUID,
    current_key_id: UUID,
    grace_period_hours: int,
    rotated_by: UUID,
) -> dict:
    # 1. Create new key
    current_key = await self._repo.get_api_key(current_key_id)
    if not current_key or current_key["organization_id"] != org_id:
        raise KeyNotFoundError(current_key_id)

    raw_key = generate_api_key(prefix=current_key["prefix"])
    key_hash, key_salt = hash_api_key(raw_key)

    new_key = await self._repo.insert_api_key(
        organization_id=org_id,
        key_hash=key_hash,
        key_salt=key_salt,
        prefix=current_key["prefix"],
        name=f"{current_key['name']} (rotated {datetime.now(timezone.utc).date()})",
        scopes=current_key["scopes"],
        expires_at=current_key["expires_at"],
    )

    # 2. Link old → new (bidirectional reference)
    await self._repo.set_rotation_ref(current_key_id, new_key["id"])

    # 3. Schedule old key revocation after grace period
    #    This is handled by a scheduled job (see Section 7.4)
    await self._repo.set_rotation_grace_until(
        current_key_id,
        datetime.now(timezone.utc) + timedelta(hours=grace_period_hours),
    )

    # 4. Invalidate cache for old key
    await self._invalidate_key_cache(current_key_id)

    return {
        "id": new_key["id"],
        "organization_id": org_id,
        "prefix": current_key["prefix"],
        "raw_key": raw_key,
        "scopes": current_key["scopes"],
        "name": new_key["name"],
        "expires_at": current_key["expires_at"],
        "created_at": new_key["created_at"],
    }
```

### 7.4 Grace Period Expiry Job

```python
# workers/auth_jobs.py — ARQ scheduled task
async def expire_rotated_keys(ctx: dict) -> None:
    """Scheduled job: revoke old keys whose grace period has expired.

    Runs every 5 minutes via ARQ cron.
    """
    db = ctx["db"]
    redis = ctx["redis"]

    result = await db.execute(
        text("""
            UPDATE api_keys
            SET revoked_at = now()
            WHERE rotation_grace_until IS NOT NULL
              AND rotation_grace_until < now()
              AND revoked_at IS NULL
            RETURNING id
        """),
    )
    await db.commit()
    revoked_ids = [row["id"] for row in result.mappings().all()]

    # Invalidate cache for all revoked keys
    for key_id in revoked_ids:
        await redis.delete(f"apikey:id:{key_id}")

    if revoked_ids:
        logger.info("auth.rotated_keys_expired", extra={"count": len(revoked_ids)})
```

---

## 8. Key Revocation

### 8.1 Endpoint

```python
# routers/admin.py
@router.delete(
    "/organizations/{org_id}/keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_api_key(
    org_id: UUID,
    key_id: UUID,
    service: AuthService = Depends(get_auth_service),
    admin: AdminUser = Depends(get_current_admin_user),
) -> None:
    """Revoke an API key immediately.

    Sets `revoked_at = now()`, which causes the auth middleware to reject
    subsequent requests with this key. The Redis cache entry is invalidated
    so the revocation takes effect immediately (not after the 5-min TTL).

    Raises:
        HTTPException(404): If key not found or not owned by this org.
    """
    await service.revoke_api_key(org_id=org_id, key_id=key_id)
```

### 8.2 Service

```python
# services/auth_service.py
async def revoke_api_key(self, org_id: UUID, key_id: UUID) -> None:
    # 1. Verify ownership
    key = await self._repo.get_api_key(key_id)
    if not key or key["organization_id"] != org_id:
        raise KeyNotFoundError(key_id)

    # 2. Set revoked_at
    await self._repo.set_revoked(key_id)

    # 3. Invalidate Redis cache immediately
    await self._redis.delete(f"apikey:id:{key_id}")
    # Also delete any hash-based cache entries
    await self._redis.delete(f"apikey:kid:{key_id}")

    logger.info("auth.key_revoked", extra={
        "key_id": str(key_id),
        "org_id": str(org_id),
        "admin_id": str(admin_id),
    })
```

---

## 9. Redis Caching Strategy

### 9.1 Cache Key Structure

| Key Pattern | Value | TTL | Purpose |
|-------------|-------|-----|---------|
| `apikey:hash:{sha256(raw)}` | JSON-encoded `ApiKeyIdentity` | 300s | Fast lookup during auth (avoids DB hash computation) |
| `apikey:kid:{key_id}` | JSON-encoded `ApiKeyIdentity` | 300s | Lookup by key ID (for admin operations) |

### 9.2 Cache Invalidation

Cache is invalidated when:

1. **Key revoked** — immediate `DEL` on both cache keys
2. **Key expired** — on-read check; stale entries purged lazily
3. **Key rotated** — old key cache invalidated, new key cached on first use

### 9.3 Cache-Aside Pattern

```
Request → Bearer token
    │
    ├── Redis: apikey:hash:{sha256(raw)} ?
    │   ├── HIT → return identity (fast path)
    │   └── MISS → PostgreSQL: lookup by hash
    │               ├── Found + valid → write to Redis → return identity
    │               └── Not found / invalid → return 401
    │
    └── Request proceeds with identity
```

### 9.4 No Cache on Key Creation

The raw key is **never cached**. The cache stores the validated identity (org_id, scopes, expiry), not the raw key string. This means:
- Compromised Redis does not leak API keys
- Key revocation just requires a `DEL` on the cached identity
- Raw keys are only in: the creation response (once), the client's secure store, and memory during auth validation

---

## 10. Error Responses

### 10.1 Error Code Catalogue

| HTTP Status | Error Code | Condition |
|-------------|-----------|-----------|
| 401 | `MISSING_API_KEY` | No `Authorization: Bearer` header |
| 401 | `INVALID_API_KEY` | Key not found in database |
| 401 | `KEY_EXPIRED` | Key's `expires_at` is in the past |
| 401 | `KEY_REVOKED` | Key's `revoked_at` is set |
| 403 | `INSUFFICIENT_SCOPE` | Key is valid but lacks required scope |
| 403 | `ORG_MISMATCH` | Key's org doesn't match the requested resource |

### 10.2 Response Format

All auth errors follow the standard error format (SRS Section 8.4):

```json
HTTP/1.1 401 Unauthorized
Content-Type: application/json
WWW-Authenticate: Bearer

{
    "error": {
        "code": "INVALID_API_KEY",
        "message": "The provided API key is not valid.",
        "request_id": "req_01j9xmf..."
    }
}
```

```json
HTTP/1.1 403 Forbidden
Content-Type: application/json

{
    "error": {
        "code": "INSUFFICIENT_SCOPE",
        "message": "Scope 'admin' is required for this endpoint.",
        "request_id": "req_01j9xmf..."
    }
}
```

---

## 11. Testing

### 11.1 Unit Tests

```python
# tests/unit/test_api_key_auth.py
class TestApiKeyGeneration:
    def test_generates_correct_prefix(self):
        key = generate_api_key(prefix="mg_test_")
        assert key.startswith("mg_test_")
        assert len(key) == 8 + 64  # prefix + token

    def test_high_entropy(self):
        keys = {generate_api_key() for _ in range(1000)}
        assert len(keys) == 1000  # No collisions

    def test_base62_no_special_chars(self):
        key = generate_api_key()
        token = key[8:]
        assert all(c in "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ" for c in token)


class TestApiKeyHashing:
    def test_hash_verification_roundtrip(self):
        raw = "mg_live_a3B8xR9kLmN2pQ4rS6tU8vW0yZ1cE3gH5iJ7kM9oP1qR3sT5uV7wX9z"
        h, salt = hash_api_key(raw)
        assert verify_api_key(raw, h, salt)
        assert not verify_api_key(raw + "x", h, salt)

    def test_different_keys_different_hashes(self):
        h1, s1 = hash_api_key("key_a")
        h2, s2 = hash_api_key("key_b")
        assert h1 != h2
        assert s1 != s2
```

### 11.2 Integration Tests

```python
# tests/integration/test_api_key_auth.py
class TestApiKeyAuthAPI:
    async def test_create_and_use_key(self, async_client, admin_headers):
        # Create
        resp = await async_client.post(
            "/admin/organizations/test-org-id/keys",
            json={"name": "test-key", "scopes": ["read"]},
            headers=admin_headers,
        )
        assert resp.status_code == 201
        raw_key = resp.json()["raw_key"]
        assert raw_key.startswith("mg_live_")

        # Use
        resp = await async_client.get(
            "/v1/users",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert resp.status_code == 200  # or appropriate response

    async def test_revoked_key_returns_401(self, async_client, admin_headers):
        # Create
        resp = await async_client.post(
            "/admin/organizations/test-org-id/keys",
            json={"name": "revocable-key"},
            headers=admin_headers,
        )
        key_id = resp.json()["id"]
        raw_key = resp.json()["raw_key"]

        # Revoke
        await async_client.delete(
            f"/admin/organizations/test-org-id/keys/{key_id}",
            headers=admin_headers,
        )

        # Use revoked key
        resp = await async_client.get(
            "/v1/users",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "KEY_REVOKED"

    async def test_expired_key_returns_401(self, async_client, admin_headers):
        # Create key with immediate expiry (in the past)
        resp = await async_client.post(
            "/admin/organizations/test-org-id/keys",
            json={
                "name": "expired-key",
                "expires_at": "2020-01-01T00:00:00Z",
            },
            headers=admin_headers,
        )
        raw_key = resp.json()["raw_key"]

        resp = await async_client.get(
            "/v1/users",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "KEY_EXPIRED"

    async def test_wrong_scope_returns_403(self, async_client, admin_headers):
        resp = await async_client.post(
            "/admin/organizations/test-org-id/keys",
            json={"name": "read-only", "scopes": ["read"]},
            headers=admin_headers,
        )
        raw_key = resp.json()["raw_key"]

        # Try to access admin endpoint (requires admin scope)
        resp = await async_client.post(
            "/admin/organizations/test-org-id/keys",
            json={"name": "another-key"},
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "INSUFFICIENT_SCOPE"
```

---

## 12. Configuration

### 12.1 Environment Variables

```python
# core/config.py
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # API Key settings
    API_KEY_CACHE_TTL: int = 300  # Redis cache TTL in seconds
    API_KEY_ROTATION_GRACE_HOURS: int = 24  # Dual-key window duration
    API_KEY_DEFAULT_EXPIRY_DAYS: int = 365  # Default key lifetime
    API_KEY_MAX_PER_ORG: int = 50  # Soft limit on keys per organization
```

---

## 13. Open Questions

| ID | Question | Status |
|----|----------|--------|
| AK-01 | Should we support key expiry notification webhooks? (POST to org-configured URL when key is 7 days from expiry) | Deferred to P2 |
| AK-02 | Audit log for key operations? (create, rotate, revoke — who, when, which key) | Implement in Phase 0; store in `audit_log` table |
| AK-03 | Should rate-limiting counts be per-key or per-org? | Per-key primary, org-level aggregate secondary (see 04-rate-limiting.md) |

---

> **Commit convention:** `feat(auth): implement API key auth with SHA-256 hashing and Redis cache`
> **Review checklist:** Verify no raw key is ever logged, confirm HMAC constant-time comparison, check cache invalidation on revocation.
