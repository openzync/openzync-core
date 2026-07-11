# ADR 003: OpenBao as Sole Source of Truth — Zero Fallback

**Status:** Accepted (2026-07-09)  
**Supersedes:** Implicit `.env`-based config for all services  

---

## Context

OpenZync previously relied on a `.env` file (or environment variables) for all
runtime configuration — database URLs, Redis URLs, JWT signing secrets, webhook
signing secrets, and tunables like log levels and rate limits.  This worked for
single-instance deployments but created several problems:

1. **Configuration drift** — every environment (dev, staging, prod) had its own
   `.env` with potentially different values, and no single source of truth.
2. **Secret sprawl** — API keys for LLM providers, webhook secrets, and JWT
   keys were scattered across files, CI/CD masked variables, and K8s secrets.
3. **No encryption at rest** — secrets stored in the database (org API keys,
   webhook secrets) were in plaintext JSONB columns.
4. **No audit trail** — secret changes left no trace of who changed what when.
5. **No key rotation** — there was no practical way to rotate encryption keys
   without downtime.

The codebase already had an OpenBao (Vault-compatible) infrastructure layer:
a Python client (`core/openbao.py`), AppRole authentication, KV v2 mounts for
system-level and per-org secrets, and a bootstrap script
(`scripts/init_openbao.sh`).  However:

- The application startup never actually called the OpenBao init path.
- `core/config.py`'s `Settings` had a dormant `init_settings_from_env()`
  fallback that was never invoked.
- The `.env` file was still the runtime source of truth, mounted directly into
  Docker containers via `env_file:` and volume mounts.
- Worker and API processes used `Settings()` with baked-in defaults for
  secrets (``DATABASE_URL`` defaulted to ``localhost``, for example) —
  masking misconfiguration until runtime.

---

## Decision

**OpenBao becomes the exclusive source of truth for ALL system configuration.**
There is zero fallback to environment variables or `.env` files.

### What changed

1. **Single `Settings` class** — `core/config.py` now defines the only
   `Settings` model.  Secret fields (`DATABASE_URL`, `REDIS_URL`,
   `SECRET_KEY`, `WEBHOOK_SIGNING_SECRET`) have no defaults — they are
   required and must be present in OpenBao KV.  Non-sensitive tunables
   retain sensible defaults.

2. **`BootstrapSettings`** — A minimal `pydantic-settings` model reads only
   three environment variables (`OZ_OPENBAO_ADDR`, `OZ_OPENBAO_ROLE_ID`,
   `OZ_OPENBAO_SECRET_ID`).  These are the ONLY env vars the system reads.
   There is no `.env` file — the bootstrap credentials come from the
   deployment environment (Docker env, K8s secrets, CI/CD masked vars).

3. **Fail-fast startup** — `services/api/asgi.py` runs `_bootstrap()` at
   import time, connecting to OpenBao and calling `init_settings()` before
   the FastAPI app is created.  If OpenBao is unreachable, the process
   exits immediately.  The Docker/K8s orchestrator handles restarts.

4. **Worker bootstrap** — `services/worker/worker.py` connects to OpenBao
   in `main()` before any other initialization, using the same fail-fast
   pattern.

5. **No env-var fallback** — `init_settings_from_env()` was removed.  The
   only valid path to create a `Settings` instance is through
   `init_settings(client: OpenBaoClient)`.

6. **Transit engine for encryption** — OpenBao's Transit secrets engine is
   enabled at `transit/` with three named keys:
   - `org-api-key` — encrypts org-level LLM/embedding API keys
   - `webhook-secret` — encrypts webhook signing secrets
   - `pii-encryption` — encrypts PII data at rest
   
   Each encrypt/decrypt operation uses the caller's UUID as additional
   authenticated data (AAD) so a ciphertext from one context cannot be
   decrypted in another.

7. **No DB dual-write** — `core/org_config.py` no longer writes a plaintext
   copy of org configuration to the `organizations.config` JSONB column.
   OpenBao is the sole store.

8. **No `.env` in deployment** — Docker Compose no longer uses `env_file`.
   The `openbao-init` container receives seed values as explicit environment
   variables and reads them from `os.environ`, not from a mounted file.

---

## Consequences

### Positive

- **Single source of truth** — every setting lives in OpenBao KV.  No more
  "works on my machine" configuration drift.
- **Encryption at rest** — org API keys and webhook secrets are encrypted
  via Transit before touching the database.
- **Key rotation** — `TransitManager.rotate_all_keys()` rotates all three
  encryption keys server-side.  Old data remains decryptable (OpenBao keeps
  previous key versions).
- **Fail-fast** — misconfigured deployments fail at startup, not when the
  first request arrives.
- **Audit trail** — OpenBao's audit log (when enabled) records every secret
  read and write.
- **Cleaner test setup** — unit tests mock `OpenBaoClient`; integration tests
  use a real OpenBao testcontainer.

### Negative

- **OpenBao is a hard dependency** — if OpenBao is down, the entire system
  is down.  Mitigation: OpenBao runs on the same Docker network with
  healthcheck-based restart, plus Raft consensus for HA in production.
- **Bootstrap credentials must be injected** — the three `OZ_OPENBAO_*` env
  vars must be present in every deployment.  This is standard practice for
  Vault/OpenBao deployments and is handled by Docker Compose, K8s secrets,
  and CI/CD masked variables.
- **`.env` is documentation-only** — developers can no longer run the stack
  without OpenBao.  In practice, `docker compose up` starts OpenBao
  automatically, and the bootstrap script seeds initial values.

---

## Alternatives Considered

### 1. Keep `.env` as the source with optional OpenBao override

Rejected.  A dual-path system creates two sources of truth, making it
impossible to reason about which value is active.  Every production incident
would start with "was it set in .env or in OpenBao?"

### 2. Use OpenBao KV only, skip Transit engine

Rejected.  Organisational API keys are stored in plaintext in the database
(via the old dual-write path) and need encryption at rest.  Transit provides
server-side encryption with automatic key rotation — far better than
application-level AES.

### 3. Keep `init_settings_from_env()` as a dev convenience

Rejected per the "zero fallback" requirement.  A dev convenience path that
bypasses OpenBao would silently mask the very failures we want to catch
early.

---

## Migration Path

The migration happened in a single release (this one):

1. All system secrets were seeded into OpenBao KV via the bootstrap script.
2. The API and worker startup were switched to OpenBao-first init.
3. The DB dual-write was removed — OpenBao is the sole org-config store.
4. The Transit engine was enabled and keys created.
5. The `.env` file was removed from the deployment contract.
6. All unit tests were updated (360 pass, 0 failures).

No rollback path is provided — the `.env`-based config is deleted from the
codebase.  To reverse, restore from version control history.
