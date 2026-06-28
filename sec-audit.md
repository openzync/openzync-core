I now have a thorough picture of every file involved. Here is the **complete remediation plan** for pre-production hardening, organized into 4 phases with concrete file-level changes.

---

## Complete Security Hardening Plan

### Phase 0: Immediate — Ship in PR #1 (Days 1-2)

#### P0.1 — Rotate all exposed credentials + purge from git history

**Rationale:** The `.env` contains live `OPENROUTER_API_KEY`, `MG_SECRET_KEY`, `MG_WEBHOOK_SIGNING_SECRET`, and a commented Supabase URL with password. These are extractable from git history despite `.gitignore` now excluding `.env`.

**Actions:**
1. Rotate `OPENROUTER_API_KEY` at openrouter.ai
2. Generate new `MG_SECRET_KEY`: `python -c "import secrets; print(secrets.token_urlsafe(48))"`
3. Generate new `MG_WEBHOOK_SIGNING_SECRET`
4. Run `git filter-repo --path .env --path streamlit_chat/.env --invert-paths` to purge from all history
5. Force-push to all branches
6. Add `streamlit_chat/.env` to `.gitignore` (append line)

**Files:**
- `.gitignore` — append `/streamlit_chat/.env`
- `.pre-commit-config.yaml` — add hook: `detect-private-key` + custom hook blocking `.env`

---

#### P0.2 — Constant-time API key verification

**Files:** `utils/crypto.py` (lines 108-124)

**Current:**
```python
computed = hashlib.sha256(f"{salt}{raw_key}".encode()).hexdigest()
return computed == stored_hash
```

**Replace with:**
```python
import hmac
computed = hashlib.sha256(f"{salt}{raw_key}".encode()).hexdigest()
return hmac.compare_digest(computed, stored_hash)
```

Remove the comment about timing attacks being impractical — constant-time is a hygiene standard regardless of network context.

---

#### P0.3 — Add security headers middleware

**New file:** `middleware/security_headers.py`

```python
class SecurityHeadersMiddleware:
    """Set security-related HTTP response headers."""
    
    SECURITY_HEADERS = {
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; ...",
        "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "strict-origin-when-cross-origin",
    }
    
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                for k, v in self.SECURITY_HEADERS.items():
                    headers.append((k.lower().encode(), v.encode()))
                message["headers"] = headers
            await send(message)
        await self.app(scope, receive, send_wrapper)
```

**Modify:** `services/api/main.py` — register after `RequestIDMiddleware` (innermost before router).

---

#### P0.4 — Move rate limit defaults from dev .env to sensible production defaults

**Current in `.env`:**
```
MG_RATE_LIMIT_IP_MAX=10000
MG_RATE_LIMIT_WINDOW_SEC=1
```
This allows 10K req/sec — effectively no rate limiting.

**Fix:** Set defaults in `core/config.py` to sensible values (10 req/60s for IP-based, configurable per org). Keep `.env` for local dev overrides.

---

### Phase 1: Short-term — Ship in PR #2 (Days 3-7)

#### P1.1 — Encrypt LLM API keys at rest (interface + dispatcher pattern)

**New files:**

1. **`core/secret_store/interface.py`** — Abstract `KeyEncryptionBackend`:
```python
class KeyEncryptionBackend(ABC):
    @abstractmethod
    async def encrypt(self, plaintext: str, context: str) -> str: ...
    @abstractmethod
    async def decrypt(self, ciphertext: str, context: str) -> str: ...
```

2. **`core/secret_store/fernet.py`** — Default AES-256-GCM implementation:
```python
class FernetKeyEncryption(KeyEncryptionBackend):
    def __init__(self, master_key: bytes):
        self._fernet = Fernet(master_key)
    
    async def encrypt(self, plaintext: str, context: str) -> str:
        # context = org_id (used as AAD if available, else plain)
        return self._fernet.encrypt(plaintext.encode()).decode()
    
    async def decrypt(self, ciphertext: str, context: str) -> str:
        return self._fernet.decrypt(ciphertext.encode()).decode()
```

3. **`core/secret_store/__init__.py`** — Dispatcher following `GraphBackendDispatcher` pattern:
```python
class SecretStoreDispatcher:
    _registry: dict[str, type[KeyEncryptionBackend]] = {}
    
    @classmethod
    def register(cls, name, backend_cls): ...
    @classmethod
    def resolve(cls, name="fernet") -> KeyEncryptionBackend: ...
```

**Modified files:**

- **`core/config.py`** — Add `MG_MASTER_ENCRYPTION_KEY` (Fernet key, 44-char base64-encoded 32 bytes)
- **`core/org_config.py`** — Hook into write path: before `update_config` stores in DB, encrypt `*_api_key` and `*_pass` fields. Hook into read path: after `get_org_config` loads from DB, decrypt them.
- **`schemas/organization_config.py`** — API keys remain `str | None` in the schema (transparent to API users). Encryption is at the persistence layer only.

**Future backends** (no code yet, just design headroom):
- `VaultSecretStore(KeyEncryptionBackend)` — reads `VAULT_ADDR`, `VAULT_TOKEN` from env
- `KmsSecretStore(KeyEncryptionBackend)` — reads `AWS_KMS_KEY_ID` from env

---

#### P1.2 — Account lockout with progressive delay

**Files:** `middleware/auth_throttle.py`, `services/auth_service.py`

**Current:** Per-email throttling (5 attempts/15 min window) that resets cleanly.

**Change to persistent lockout counters:**
```python
async def check_login_attempt(self, email: str, ip: str) -> dict:
    acct_key = f"auth:lockout:acct:{email}"
    attempts = await self._redis.incr(acct_key)
    await self._redis.expire(acct_key, 3600)  # 1h window
    
    lockout_minutes = 0
    if attempts >= 20: lockout_minutes = 60
    elif attempts >= 15: lockout_minutes = 15
    elif attempts >= 10: lockout_minutes = 5
    elif attempts >= 5: lockout_minutes = 1
    
    if lockout_minutes > 0:
        raise RateLimitError(f"Account locked for {lockout_minutes} minute(s).")
    return {}
```

**Also:** Return a `captcha_required` flag at ≥5 attempts (for CAPTCHA integration in Phase 2).

---

#### P1.3 — Strengthen password validation (OWASP L2)

**File:** `services/auth_service.py:362-374`, `schemas/auth.py:25-31`

**Current:** `len(password) < 8`

**Replace with:**
```python
import re

def _validate_password(password: str) -> None:
    if len(password) < 12:
        raise ValidationError("Password must be at least 12 characters long.")
    if not re.search(r"[A-Z]", password):
        raise ValidationError("Password must contain an uppercase letter.")
    if not re.search(r"[a-z]", password):
        raise ValidationError("Password must contain a lowercase letter.")
    if not re.search(r"\d", password):
        raise ValidationError("Password must contain a digit.")
    if not re.search(r"[!@#$%^&*(),.?\":{}|<>_\-]", password):
        raise ValidationError("Password must contain a special character.")
```

**Optional enhancement:** Add `pip install zxcvbn` and check against common-password lists.

**Also update** `schemas/auth.py:SignupRequest.password.min_length` from 8 to 12.

---

#### P1.4 — Email verification flow

**New files:**
- `workers/email_jobs.py` — RQ job sending verification email via SMTP/SendGrid

**Modified files:**
- `schemas/auth.py` — Add `email_verified: bool` to user response schemas
- `services/auth_service.py` — On signup: set `email_verified=False` in user creation, generate SHA-256 verification token, store hash in `dashboard_users.verification_token_hash` (new column), enqueue verification email job
- `routers/auth.py` — Add `GET /v1/auth/verify?token=...` endpoint
- **Alembic migration** — Add `verification_token_hash`, `email_verified` columns to `dashboard_users`
- `middleware/auth.py` — Block API key creation for unverified users (in `PUBLIC_ENDPOINTS` or in service layer)

**Files to create:**
- `prompts/email_verification.jinja2` — email template

---

#### P1.5 — Switch JWT from HS256 to ES256

**Files:** `utils/crypto.py`, `core/config.py`

**Generate P-256 key pair** (run once, commit public key, store private key in env):
```bash
openssl ecparam -name prime256v1 -genkey -noout -out private.pem
openssl ec -in private.pem -pubout -out public.pem
```

**Config additions** (`core/config.py`):
```python
JWT_PRIVATE_KEY: str = Field(..., validation_alias="MG_JWT_PRIVATE_KEY")
JWT_PUBLIC_KEY: str = Field(..., validation_alias="MG_JWT_PUBLIC_KEY")
```

**Updated `create_jwt_token`**:
```python
def create_jwt_token(data, private_key: str, expires_delta) -> str:
    private_key_obj = serialization.load_pem_private_key(
        private_key.encode(), password=None
    )
    return jwt.encode(to_encode, private_key_obj, algorithm="ES256")
```

**Updated `verify_jwt_token`**:
```python
def verify_jwt_token(token: str, public_key: str) -> dict:
    public_key_obj = serialization.load_pem_public_key(public_key.encode())
    return jwt.decode(token, public_key_obj, algorithms=["ES256"])
```

**Breaking change:** All existing tokens invalid after deploy. Acceptable in pre-production.

---

### Phase 2: Medium-term — Ship in PR #3 (Weeks 2-4)

| # | Task | Files | Key changes |
|---|------|-------|-------------|
| **P2.1** | Redis auth + separate instances | `core/redis.py`, `.env.example`, `.gitlab-ci.yml` | Add `requirepass` to Redis, connect via `redis://:password@host:port/0`. Deploy separate FalkorDB instance on a different port with its own password. Update CI services config to match. |
| **P2.2** | Circuit breaker for LLM providers | `core/llm_backends.py` | Add `pybreaker.CircuitBreaker` around each backend's `_chat()` and `embed()` calls. Config: 5 failures → open for 30s, half-open after 15s. Log state transitions. |
| **P2.3** | CAPTCHA on auth endpoints | `routers/auth.py`, `middleware/auth_throttle.py`, `schemas/auth.py` | Integrate Cloudflare Turnstile. Add `cf-turnstile-response` header validation on login (when `captcha_required` flag is set) and always on signup. |
| **P2.4** | PII redaction in audit log response body capture | `schemas/organization_config.py`, `middleware/audit.py` | Before storing response body, run through a redaction filter that strips `api_key`, `password`, `secret`, `token` patterns. Config-driven via `audit_log_response_body` field. |
| **P2.5** | Dependency scanning in CI main pipeline | `.gitlab-ci.yml` | Move `pip-audit` from `schedules` to the `test-unit` stage so it runs on every commit, not just weekly. |
| **P2.6** | Rate limit hardening (Redis-down guard) | `middleware/rate_limit.py` | When Redis is unreachable, return 503 instead of silently allowing. Add `allow_on_redis_fail` config flag (default `False` in production). |

---

### Phase 3: Long-term — Ship in PR #4 (Month 2+)

| # | Task | Scope |
|---|------|-------|
| **P3.1** | HashiCorp Vault integration | Implement `VaultSecretStore(KeyEncryptionBackend)`. Register as `"vault"` in the dispatcher. Migrate from Fernet to Vault by toggling `MG_SECRET_STORE_BACKEND=vault`. |
| **P3.2** | Session management UI | Dashboard page to view active sessions (derived from refresh token table) and revoke sessions remotely. |
| **P3.3** | Per-endpoint rate limiting | Replace the single `rate:api:{org_id}` key with per-route keys (`rate:api:{org_id}:{path}`). Define rate tiers in org config. |
| **P3.4** | 2FA (WebAuthn/TOTP) | Add `POST /v1/auth/2fa/setup`, `POST /v1/auth/2fa/verify` endpoints. Store TOTP secret encrypted via the `KeyEncryptionBackend`. Require 2FA for admin roles. |

---

### Dependency graph between phases

```
Phase 0 ─────────────────────────────────────────────────
  │
  ├── P0.1 (credential rotation) → needed before P1.1 (encryption of 
  │   already-rotated keys makes sense; encrypting already-leaked keys is a no-op)
  │
  ├── P0.5 (constant-time) → independent, could ship as fast-follow
  │
  └── P0.6 (security headers) → independent, merge anytime
  │
Phase 1 ─────────────────────────────────────────────────
  │
  ├── P1.1 (key encryption) → depends on P0.1
  ├── P1.2 (account lockout) → depends on P0.4 (rate-limit defaults)
  ├── P1.3 (password policy) → independent
  ├── P1.4 (email verification) → needs P0.1 (SMTP credentials)
  └── P1.5 (JWT ES256) → depends on P0.1 (key rotation)
  │
Phase 2 ─────────────────────────────────────────────────
  │
  ├── P2.1 (Redis auth) → independent, can ship anytime
  ├── P2.2 (circuit breaker) → independent
  ├── P2.3 (CAPTCHA) → depends on P1.2 (lockout flag)
  ├── P2.4 (audit redaction) → depends on P1.1 (decrypt before redacting)
  └── P2.5-2.6 (CI hardening) → independent
  │
Phase 3 ─────────────────────────────────────────────────
  └── All depend on Phase 1-2 infrastructure being stable
```

---
