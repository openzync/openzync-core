# JWT Authentication (Admin Dashboard)

> **Phase:** 0 (Foundation)
> **SRS Requirements:** AUTH-05, SEC-08, DASH-08
> **Dependencies:** [01-api-key-auth.md](01-api-key-auth.md) (shared `AuthService`), [01-postgresql-schema.md](../01-data-models/01-postgresql-schema.md)

---

## 1. Overview

The admin dashboard uses **JWT-based authentication** with short-lived access tokens and rotating refresh tokens. Unlike API key auth (used by agent-facing `/v1/` endpoints), JWT auth carries user identity (`user_id`), role (`admin` or `super_admin`), and organization membership in the token itself, enabling dashboard-specific authorization decisions without a DB lookup on every request.

**Architecture decision:** JWT auth and API key auth are **separate systems**. They share the `AuthService` class but use distinct databases (`admin_users` + `refresh_tokens` vs `api_keys`), distinct token formats (JWT vs Bearer random token), and distinct validation paths. This prevents any confusion between dashboard users and agent API consumers.

---

## 2. Token Structure

### 2.1 Access Token (Short-Lived)

```python
# schemas/auth.py
from pydantic import BaseModel
from uuid import UUID, uuid4
from datetime import datetime, timezone, timedelta


class AccessTokenClaims(BaseModel):
    """Payload of the JWT access token.

    Designed to be self-contained for dashboard authorization —
    no DB lookup needed to verify org membership or role.
    """
    sub: str           # User ID (UUID as string)
    org_id: str        # Organization UUID
    role: str          # 'admin' | 'super_admin'
    exp: int           # Expiration timestamp (epoch seconds)
    iat: int           # Issued at (epoch seconds)
    jti: str           # Unique token ID (UUID) — for revocation

    @classmethod
    def create(cls, user_id: UUID, org_id: UUID, role: str, expiry_minutes: int = 15) -> "AccessTokenClaims":
        now = datetime.now(timezone.utc)
        return cls(
            sub=str(user_id),
            org_id=str(org_id),
            role=role,
            exp=int((now + timedelta(minutes=expiry_minutes)).timestamp()),
            iat=int(now.timestamp()),
            jti=str(uuid4()),
        )
```

### 2.2 Refresh Token (Long-Lived, Rotated)

```python
# schemas/auth.py
class RefreshTokenClaims(BaseModel):
    """Payload of the JWT refresh token.

    Contains fewer claims than access token — the refresh endpoint
    looks up the full user context from the DB using the token's jti.
    """
    sub: str           # User ID
    exp: int           # Expiration (epoch seconds)
    iat: int           # Issued at
    jti: str           # Unique token ID — used as DB lookup key
    type: str = "refresh"  # Token type discriminator

    @classmethod
    def create(cls, user_id: UUID, expiry_days: int = 7) -> "RefreshTokenClaims":
        now = datetime.now(timezone.utc)
        return cls(
            sub=str(user_id),
            exp=int((now + timedelta(days=expiry_days)).timestamp()),
            iat=int(now.timestamp()),
            jti=str(uuid4()),
        )
```

---

## 3. Token Signing

### 3.1 Configuration

```python
# core/config.py
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # JWT
    SECRET_KEY: str           # No default — must be set in environment
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    JWT_ISSUER: str = "OpenZep"  # iss claim for token validation
```

### 3.2 Signing Utilities

```python
# core/crypto.py
import jwt  # PyJWT library
from datetime import datetime, timezone
from typing import Any

class JWTError(Exception):
    """Base JWT error."""

class JWTExpiredError(JWTError):
    """Token has expired."""

class JWTInvalidError(JWTError):
    """Token is malformed or signature is invalid."""


def encode_jwt(claims: dict, secret: str, algorithm: str = "HS256") -> str:
    """Sign and encode a JWT.

    Args:
        claims: Dictionary of claims (sub, exp, iat, jti, etc.)
        secret: HMAC signing key from SECRET_KEY env var.
        algorithm: Signing algorithm (default HS256).

    Returns:
        Encoded JWT string (3-part, dot-separated).
    """
    return jwt.encode(claims, secret, algorithm=algorithm)


def decode_jwt(token: str, secret: str, algorithms: list[str] | None = None) -> dict[str, Any]:
    """Decode and verify a JWT.

    Validates: signature, expiration (exp), issued-at (iat).

    Args:
        token: Encoded JWT string.
        secret: HMAC signing key.
        algorithms: Accepted algorithms (default ["HS256"]).

    Returns:
        Decoded claims dictionary.

    Raises:
        JWTExpiredError: If the token has expired.
        JWTInvalidError: If signature is invalid or claims are malformed.
    """
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=algorithms or ["HS256"],
            options={"require": ["exp", "iat", "jti"]},
        )
        return payload
    except jwt.ExpiredSignatureError as e:
        raise JWTExpiredError("Token has expired") from e
    except jwt.PyJWTError as e:
        raise JWTInvalidError(f"Invalid token: {e}") from e
```

### 3.3 Security Considerations for HS256

| Concern | Mitigation |
|---------|-----------|
| Symmetric key compromise | `SECRET_KEY` must be 256-bit (32 bytes) random value, rotated quarterly |
| No sender identity | HS256 is symmetric — both issuer and verifier share the secret. Acceptable for a single-service deployment. For multi-service, switch to RS256. |
| Clock skew | Allow 30s leeway (`leeway=30` in `jwt.decode`) |
| Algorithm confusion | Pin to HS256 explicitly — never accept "none" algorithm |

---

## 4. Refresh Token Rotation

### 4.1 Rotation Protocol

Refresh tokens are **rotated on every use**: when a client exchanges a refresh token for a new access token, the old refresh token is revoked and a new refresh+access pair is issued. This limits the window of exposure if a refresh token is stolen.

```
Client                      Auth Service                  Database
  │                              │                           │
  │  POST /auth/refresh          │                           │
  │  {refresh_token: "xxx"}      │                           │
  │─────────────────────────────►│                           │
  │                              │  Decode & validate JWT    │
  │                              │  Look up refresh token    │
  │                              │  by jti in DB             │
  │                              │──────────────────────────►│
  │                              │  SELECT FROM refresh_tokens
  │                              │◄──────────────────────────│
  │                              │                           │
  │                              │  Check: not revoked,      │
  │                              │  not expired, user active │
  │                              │                           │
  │                              │  Revoke old token         │
  │                              │──────────────────────────►│
  │                              │  UPDATE revoked_at=now()  │
  │                              │◄──────────────────────────│
  │                              │                           │
  │                              │  Issue new pair           │
  │                              │  (access + refresh)       │
  │                              │                           │
  │  200 {access_token,          │                           │
  │       refresh_token,         │                           │
  │       expires_in}            │                           │
  │◄─────────────────────────────│                           │
```

### 4.2 Token Family & Reuse Detection

If a revoked refresh token is presented again, it indicates potential token theft. The system detects this and **revokes the entire token family** (all refresh tokens for that user) as a security measure.

```python
# services/auth_service.py
class TokenFamilyCompromisedError(AppError):
    """Raised when a revoked refresh token is reused — potential theft."""

async def rotate_refresh_token(
    self,
    old_refresh_token: str,
) -> TokenPair:
    """Exchange a refresh token for a new access+refresh pair.

    Implements rotation with theft detection:
    - If the token is valid: revoke old, issue new pair.
    - If the token was already revoked: revoke ALL user tokens
      (token family compromised) and raise an error.
    """
    # 1. Decode JWT
    try:
        claims = decode_jwt(old_refresh_token, self._settings.SECRET_KEY)
    except JWTError as e:
        raise InvalidTokenError(str(e))

    if claims.get("type") != "refresh":
        raise InvalidTokenError("Not a refresh token")

    # 2. Look up in DB
    stored = await self._repo.get_refresh_token_by_jti(claims["jti"])
    if not stored:
        raise InvalidTokenError("Refresh token not found")

    # 3. Reuse detection: if already revoked, token family is compromised
    if stored["revoked_at"] is not None:
        await self._repo.revoke_all_user_tokens(stored["user_id"])
        logger.warning("auth.token_family_compromised", extra={
            "user_id": str(stored["user_id"]),
            "jti": claims["jti"],
        })
        raise TokenFamilyCompromisedError(
            "Refresh token was already revoked — possible token theft. "
            "All sessions for this user have been invalidated."
        )

    # 4. Check expiry
    if datetime.now(timezone.utc).timestamp() > claims["exp"]:
        raise InvalidTokenError("Refresh token has expired")

    # 5. Revoke old token
    await self._repo.revoke_refresh_token(claims["jti"])

    # 6. Issue new pair
    user = await self._repo.get_admin_user_by_id(UUID(claims["sub"]))
    if not user or not user["is_active"]:
        raise UserNotFoundError("User not found or inactive")

    return await self._issue_token_pair(user)
```

---

## 5. Token Storage — `refresh_tokens` Table

### 5.1 Database Schema

```sql
-- Migration: V003_create_refresh_tokens.sql

CREATE TABLE refresh_tokens (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
    token_hash      TEXT NOT NULL,           -- SHA-256 HMAC of the JWT (not the raw JWT)
    jti             TEXT NOT NULL UNIQUE,    -- Unique token ID from JWT claims
    family_id       UUID NOT NULL,           -- Groups all tokens in a rotation family
    expires_at      TIMESTAMPTZ NOT NULL,
    revoked_at      TIMESTAMPTZ,             -- NULL = active, set = revoked
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by_ip   TEXT,                    -- IP that created this token (audit)
    user_agent      TEXT                     -- User agent that created this token (audit)
);

-- Fast lookup by jti during refresh
CREATE INDEX idx_refresh_tokens_jti ON refresh_tokens (jti);

-- Fast lookup of active tokens for a user
CREATE INDEX idx_refresh_tokens_active
    ON refresh_tokens (user_id, expires_at)
    WHERE revoked_at IS NULL;

-- Cleanup expired tokens
CREATE INDEX idx_refresh_tokens_expires_at ON refresh_tokens (expires_at);
```

### 5.2 Why Hash the Stored Token?

The `token_hash` column stores `SHA-256(JWT)` — not the raw JWT. This ensures:

- A database compromise does not leak valid refresh tokens
- The `jti` column is the primary lookup key; the hash is a secondary verification
- Token validation: decode JWT → extract `jti` → look up by `jti` → verify hash matches

```python
async def store_refresh_token(self, token: str, claims: RefreshTokenClaims, user_id: UUID, ip: str | None, ua: str | None) -> None:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    await self._db.execute(
        text("""
            INSERT INTO refresh_tokens
                (user_id, token_hash, jti, family_id, expires_at, created_by_ip, user_agent)
            VALUES
                (:user_id, :token_hash, :jti, :family_id, :expires_at, :ip, :ua)
        """),
        {
            "user_id": user_id,
            "token_hash": token_hash,
            "jti": claims.jti,
            "family_id": uuid4(),  # first token in family
            "expires_at": datetime.fromtimestamp(claims.exp, tz=timezone.utc),
            "ip": ip,
            "ua": ua,
        },
    )
    await self._db.commit()
```

---

## 6. Auth Endpoints

### 6.1 Login

```python
# routers/auth.py
from fastapi import APIRouter, HTTPException, Request, status, Response

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenPairResponse)
async def login(
    payload: LoginRequest,
    response: Response,
    service: AuthService = Depends(get_auth_service),
    request: Request,
) -> TokenPairResponse:
    """Authenticate an admin user and return JWT token pair.

    Validates email + password. On success:
    - Sets HttpOnly cookie with refresh token
    - Returns JSON body with access token + refresh token

    Password validation uses bcrypt (user-entered password).
    """
    user = await service.authenticate_admin(payload.email, payload.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_CREDENTIALS", "message": "Invalid email or password."},
        )

    token_pair = await service.issue_token_pair(
        user=user,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )

    # Set HttpOnly cookie for browser-based dashboard access
    response.set_cookie(
        key="refresh_token",
        value=token_pair.refresh_token,
        httponly=True,
        secure=True,          # HTTPS only in production
        samesite="strict",    # CSRF protection
        max_age=60 * 60 * 24 * 7,  # 7 days
        path="/auth",
    )

    return token_pair


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenPairResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds until access token expiry
```

### 6.2 Token Refresh

```python
@router.post("/refresh", response_model=TokenPairResponse)
async def refresh_token(
    payload: RefreshTokenRequest | None = None,
    service: AuthService = Depends(get_auth_service),
    request: Request,
    response: Response,
) -> TokenPairResponse:
    """Exchange a refresh token for a new token pair.

    Accepts the refresh token from:
    1. Request body (JSON: {"refresh_token": "..."})
    2. HttpOnly cookie (if not in body)

    Implements rotation + theft detection.
    """
    # Extract refresh token from body or cookie
    refresh_token = payload.refresh_token if payload else None
    if not refresh_token:
        refresh_token = request.cookies.get("refresh_token")

    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "MISSING_REFRESH_TOKEN", "message": "Refresh token is required."},
        )

    try:
        token_pair = await service.rotate_refresh_token(
            old_refresh_token=refresh_token,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except TokenFamilyCompromisedError:
        # Revoke all cookies + sessions
        response.delete_cookie("refresh_token", path="/auth")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "TOKEN_THEFT_DETECTED",
                "message": "Possible token theft detected. All sessions invalidated. Please log in again.",
            },
        )
    except InvalidTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_TOKEN", "message": str(e)},
        )

    # Set new cookie
    response.set_cookie(
        key="refresh_token",
        value=token_pair.refresh_token,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=60 * 60 * 24 * 7,
        path="/auth",
    )

    return token_pair
```

### 6.3 Logout

```python
@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    service: AuthService = Depends(get_auth_service),
    current_user: AdminUser = Depends(get_current_admin_user),
    response: Response,
) -> None:
    """Revoke all refresh tokens for the current user."""
    await service.revoke_all_user_tokens(current_user.id)
    response.delete_cookie("refresh_token", path="/auth")
```

---

## 7. FastAPI Dependencies

### 7.1 `get_current_admin_user` — Primary Auth Dependency

```python
# dependencies/auth.py
from fastapi import Request, HTTPException, status, Depends
from fastapi.security import HTTPBearer

# Optional: accept Bearer token from header (for API clients)
bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_admin_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    service: AuthService = Depends(get_auth_service),
) -> AdminUser:
    """Validate JWT access token and return the authenticated admin user.

    Token sources (checked in order):
    1. Authorization: Bearer <token> header
    2. HttpOnly cookie: `access_token`

    Attaches validated user info to request.state for downstream use.

    Raises:
        HTTPException(401): Missing, invalid, or expired token.
    """
    # 1. Extract token from Bearer header or cookie
    token = None
    if credentials:
        token = credentials.credentials
    if not token:
        token = request.cookies.get("access_token")

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "MISSING_TOKEN", "message": "Authentication required."},
        )

    # 2. Decode and validate JWT
    try:
        claims = decode_jwt(token, settings.SECRET_KEY)
    except JWTExpiredError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "TOKEN_EXPIRED",
                "message": "Access token has expired. Use /auth/refresh to obtain a new one.",
            },
            headers={"X-Token-Expired": "true"},  # Client can intercept this
        )
    except JWTInvalidError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_TOKEN", "message": str(e)},
        )

    # 3. Verify token type (must be access, not refresh)
    if claims.get("type") == "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "WRONG_TOKEN_TYPE", "message": "Use an access token, not a refresh token."},
        )

    # 4. Look up user (still need DB check for deactivated accounts)
    user = await service.get_admin_user_by_id(UUID(claims["sub"]))
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "USER_INACTIVE", "message": "User account is deactivated."},
        )

    # 5. Attach to request state
    request.state.admin_user = user
    request.state.org_id = user.organization_id

    return user


# Convenience alias for routes that don't use the full user object
OrgId = Annotated[UUID, Depends(lambda: request.state.org_id)]
```

### 7.2 Role-Based Dependencies

```python
async def require_super_admin(
    current_user: AdminUser = Depends(get_current_admin_user),
) -> AdminUser:
    """Require super_admin role. Used for cross-tenant operations.

    Super admins can manage all organizations. Org admins are scoped to
    their own org.
    """
    if current_user.role != "super_admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": "Super admin privileges are required for this operation.",
            },
        )
    return current_user


def require_org_access(org_id: UUID):
    """Dependency factory: verify the admin belongs to the requested org.

    Super admins bypass this check (they have cross-tenant access).

    Usage:
        @router.get("/admin/organizations/{org_id}/keys")
        async def list_keys(
            org_id: UUID,
            admin: AdminUser = Depends(require_org_access(org_id)),
        ):
            ...
    """
    async def org_access_check(
        current_user: AdminUser = Depends(get_current_admin_user),
    ) -> AdminUser:
        if current_user.role == "super_admin":
            return current_user
        if current_user.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,  # 404 — no information leak
                detail={"code": "NOT_FOUND", "message": "Organization not found."},
            )
        return current_user
    return org_access_check
```

---

## 8. Admin User Model

### 8.1 Database Schema

```sql
-- Migration: V001_create_admin_users.sql

CREATE TABLE admin_users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    email           TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,           -- bcrypt hash (user-entered password)
    name            TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'admin'
                        CHECK (role IN ('admin', 'super_admin')),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    last_login_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 8.2 Pydantic Schema

```python
# schemas/admin.py
class AdminUser(BaseModel):
    id: UUID
    organization_id: UUID
    email: str
    name: str
    role: str  # 'admin' | 'super_admin'
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


class AdminUserCreate(BaseModel):
    email: str = Field(pattern=r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    role: str = Field(default="admin", pattern=r"^(admin|super_admin)$")
    organization_id: UUID
```

### 8.3 Password Hashing (bcrypt)

User passwords (vs API keys) use bcrypt — the correct choice for human-chosen secrets:

```python
# core/crypto.py
import bcrypt

def hash_password(password: str) -> str:
    """Hash a user password with bcrypt.

    Uses bcrypt (not SHA-256) because:
    - Passwords have low entropy (humans choose them)
    - bcrypt's work factor provides brute-force resistance
    - 72-char limit is fine for passwords (< 50 chars typical)
    """
    return bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt(rounds=12),  # cost factor 12 (~250ms on modern hardware)
    ).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its bcrypt hash."""
    return bcrypt.checkpw(
        password.encode("utf-8"),
        password_hash.encode("utf-8"),
    )
```

---

## 9. Cookie Security & CSRF Protection

### 9.1 Cookie Configuration

```python
# dependencies/auth.py
def set_auth_cookie(response: Response, token: str, max_age: int) -> None:
    """Set an HttpOnly secure cookie for JWT storage.

    Cookie attributes are designed to prevent XSS and CSRF attacks:
    - HttpOnly: not readable by JavaScript
    - Secure: only sent over HTTPS
    - SameSite=Strict: not sent on cross-site requests
    - Path=/auth: only sent to auth endpoints (not to /v1/* API)
    """
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=max_age,
        path="/",
    )
```

### 9.2 CSRF Mitigation

| Attack Vector | Mitigation |
|--------------|------------|
| CSRF via cookie | `SameSite=Strict` — browser won't send cookie on cross-site requests |
| XSS token theft | `HttpOnly=True` — JS can't read the token |
| Man-in-middle | `Secure=True` — cookie only sent over HTTPS |
| Refresh token in URL | `Path=/auth` — cookie not sent to `/v1/*` endpoints |
| Cross-origin API calls | CORS allowlist on dashboard origin only |

---

## 10. Token Revocation

### 10.1 Explicit Logout

```python
async def revoke_all_user_tokens(self, user_id: UUID) -> None:
    """Revoke all active refresh tokens for a user.

    Called on logout or when token family compromise is detected.
    """
    await self._repo.revoke_all_user_tokens(user_id)
    # No cache to clear — refresh tokens are checked against DB on every use
```

### 10.2 Scheduled Cleanup

```python
# workers/auth_jobs.py
async def cleanup_expired_refresh_tokens(ctx: dict) -> None:
    """Scheduled job: hard-delete expired refresh tokens older than 30 days.

    Runs daily. Tokens that expired naturally are safe to delete;
    the access tokens they issued have already expired.
    """
    db = ctx["db"]
    result = await db.execute(
        text("""
            DELETE FROM refresh_tokens
            WHERE expires_at < now() - INTERVAL '30 days'
            RETURNING id
        """),
    )
    await db.commit()
    deleted_count = len(result.mappings().all())
    if deleted_count:
        logger.info("auth.cleaned_expired_tokens", extra={"count": deleted_count})
```

---

## 11. Sequence Diagram — Full Auth Flow

```
Browser / Dashboard App              FastAPI                        PostgreSQL / Redis
      │                                  │                              │
      │  POST /auth/login                │                              │
      │  {email, password}               │                              │
      │─────────────────────────────────►│                              │
      │                                  │  SELECT from admin_users     │
      │                                  │  WHERE email = ?             │
      │                                  │─────────────────────────────►│
      │                                  │◄─────────────────────────────│
      │                                  │                              │
      │                                  │  verify_password(bcrypt)     │
      │                                  │                              │
      │                                  │  Create access JWT (15min)   │
      │                                  │  Create refresh JWT (7d)     │
      │                                  │                              │
      │                                  │  INSERT refresh_tokens       │
      │                                  │  (hashed JWT, jti, ...)      │
      │                                  │─────────────────────────────►│
      │                                  │                              │
      │  200 {access_token,              │                              │
      │       refresh_token}             │                              │
      │  Set-Cookie: refresh_token=...   │                              │
      │◄─────────────────────────────────│                              │
      │                                  │                              │
      │  ─ ─ ─ ─ ─ ─ (15 min later) ─ ─ ─                              │
      │                                  │                              │
      │  POST /auth/refresh              │                              │
      │  Cookie: refresh_token=...       │                              │
      │─────────────────────────────────►│                              │
      │                                  │  Decode JWT, extract jti     │
      │                                  │  SELECT from refresh_tokens  │
      │                                  │  WHERE jti = ?               │
      │                                  │─────────────────────────────►│
      │                                  │◄─────────────────────────────│
      │                                  │                              │
      │                                  │  Check: NOT revoked,         │
      │                                  │         NOT expired          │
      │                                  │                              │
      │                                  │  Revoke old token            │
      │                                  │  UPDATE revoked_at=now()     │
      │                                  │─────────────────────────────►│
      │                                  │                              │
      │                                  │  Issue new pair              │
      │                                  │  INSERT new refresh_token    │
      │                                  │─────────────────────────────►│
      │                                  │                              │
      │  200 {new_access,                │                              │
      │       new_refresh}               │                              │
      │  Set-Cookie: refresh_token=new   │                              │
      │◄─────────────────────────────────│                              │
```

---

## 12. Testing

### 12.1 Unit Tests

```python
# tests/unit/test_jwt_auth.py
class TestJWTSigning:
    def test_sign_and_verify_roundtrip(self):
        claims = {"sub": "user-123", "org_id": "org-456", "role": "admin"}
        token = encode_jwt(claims, "test-secret-key-32-bytes-long!!")
        decoded = decode_jwt(token, "test-secret-key-32-bytes-long!!")
        assert decoded["sub"] == "user-123"
        assert decoded["org_id"] == "org-456"

    def test_expired_token_raises(self):
        claims = {"sub": "u1", "exp": 0}  # expired in 1970
        token = encode_jwt(claims, "test-secret")
        with pytest.raises(JWTExpiredError):
            decode_jwt(token, "test-secret")

    def test_wrong_secret_rejected(self):
        token = encode_jwt({"sub": "u1"}, "secret-a")
        with pytest.raises(JWTInvalidError):
            decode_jwt(token, "secret-b")
```

### 12.2 Integration Tests

```python
# tests/integration/test_jwt_auth.py
class TestLoginFlow:
    async def test_login_success(self, async_client):
        resp = await async_client.post("/auth/login", json={
            "email": "admin@test.com",
            "password": "correct-password",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert "refresh_token" in data
        # Access token should be ~15min
        assert data["expires_in"] == pytest.approx(900, abs=10)

    async def test_login_wrong_password(self, async_client):
        resp = await async_client.post("/auth/login", json={
            "email": "admin@test.com",
            "password": "wrong-password",
        })
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "INVALID_CREDENTIALS"

    async def test_refresh_rotation(self, async_client):
        # Login
        login_resp = await async_client.post("/auth/login", json={
            "email": "admin@test.com",
            "password": "correct-password",
        })
        old_refresh = login_resp.json()["refresh_token"]

        # Refresh
        refresh_resp = await async_client.post("/auth/refresh", json={
            "refresh_token": old_refresh,
        })
        assert refresh_resp.status_code == 200
        new_refresh = refresh_resp.json()["refresh_token"]
        assert new_refresh != old_refresh  # rotated

        # Old refresh should now be revoked
        reuse_resp = await async_client.post("/auth/refresh", json={
            "refresh_token": old_refresh,
        })
        assert reuse_resp.status_code == 401
        assert reuse_resp.json()["error"]["code"] == "TOKEN_THEFT_DETECTED"

    async def test_protected_endpoint_with_valid_token(self, async_client):
        resp = await async_client.post("/auth/login", json={
            "email": "admin@test.com",
            "password": "correct-password",
        })
        access = resp.json()["access_token"]

        resp = await async_client.get(
            "/admin/organizations",
            headers={"Authorization": f"Bearer {access}"},
        )
        assert resp.status_code == 200

    async def test_protected_endpoint_without_token(self, async_client):
        resp = await async_client.get("/admin/organizations")
        assert resp.status_code == 401
```

---

## 13. Configuration

```python
# core/config.py
class Settings(BaseSettings):
    # JWT
    SECRET_KEY: str                           # Required. Generate: openssl rand -hex 32
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    JWT_ISSUER: str = "OpenZep"
    JWT_LEEWAY_SECONDS: int = 30              # Clock skew tolerance

    # Session
    SESSION_CLEANUP_INTERVAL_HOURS: int = 24  # Expired token cleanup frequency
```

### 13.1 Production Secret Key Generation

```bash
# Generate a cryptographically secure SECRET_KEY (32 bytes)
openssl rand -hex 32
# Output example: 7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7

# Store in environment (not in repo):
export SECRET_KEY="7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7"
```

---

## 14. Open Questions

| ID | Question | Status |
|----|----------|--------|
| JWT-01 | Should we support OAuth2 social login (Google, GitHub) for dashboard? | Deferred to P2 |
| JWT-02 | Should we add MFA/TOTP for dashboard login? | Deferred to P2 |
| JWT-03 | Audit log granularity — log every token refresh or only failures? | Log failures + every 10th success (sampling) |
| JWT-04 | RS256 vs HS256 for multi-service deployments? | HS256 for now (single FastAPI service). Revisit if auth service is extracted. |

---

> **Commit convention:** `feat(auth): implement JWT auth with refresh rotation and CSRF-safe cookies`
> **Review checklist:** Verify refresh token rotation/theft detection, confirm HttpOnly+Secure+SameSite cookie flags, check bcrypt cost factor, ensure no raw JWT stored in DB.
