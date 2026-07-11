# ADR 004: Self-Bootstrapping Postgres via OpenBao-Injected Credentials

**Status:** Accepted (2026-07-11)  
**Supersedes:** Manual `OZ_DATABASE_URL` env var + pre-deployment Alembic step  

---

## Context

The OpenZync docker-compose stack currently requires the operator to
provide `OZ_DATABASE_URL` BEFORE deployment.  The init script
(`init_openbao.sh`) writes that URL to OpenBao's KV store blindly —
it cannot verify the URL works because the database isn't started
yet.  This is a chicken-and-egg problem: the operator must know the
database URL, the database password, AND bootstrap the database, all
in the right order, all before `docker compose up`.

The `.env` file currently holds three classes of secret that should
not live together: the DB password (embedded in `OZ_DATABASE_URL`),
the OpenBao bootstrap credentials (`OZ_OPENBAO_ROLE_ID` /
`OZ_OPENBAO_SECRET_ID`), and the AppRole credentials for the api and
worker, which the bootstrap script PRINTS to logs and the operator
must then copy-paste into `.env` — error-prone and a security smell.
The Alembic migration step is also a manual `make migrate`
invocation with no orchestration and no healthcheck.  The end
result: a single `docker compose up -d` does NOT produce a working
OpenZync stack.  An operator must run `make bootstrap-openbao`,
wait, copy the AppRole credentials, run `make migrate`, then
`make up`.  This violates the spirit of ADR-003 (OpenBao-zero-
fallback): the DB password still sits in `.env` alongside the
OpenBao bootstrap credentials, and a successful boot still requires
operator intervention between phases.

---

## Decision

**Postgres becomes self-bootstrapping.**  The database, its users,
its passwords, and its migration step are all created and
orchestrated INSIDE the docker-compose stack.  All secrets are
written to OpenBao (never `.env`) and injected into the api and
worker via OpenBao Agent sidecars.  A single `docker compose up -d`
produces a working OpenZync stack with zero operator intervention.

### Phased startup sequence

The stack boots in eight strictly-ordered phases.  Each phase has a
hard dependency on the previous one; docker-compose
`depends_on: condition: service_healthy` enforces the order at the
orchestrator level (no race conditions, no "first request fails"
surprises).

1. **Phase 1 — OpenBao bootstrap.**  `openbao` starts first, seeded
   only with `BAO_STATIC_SEAL_KEY` from the deployment environment
   (Docker env, K8s secret — never `.env`).  The `openbao-init`
   sidecar waits for OpenBao's healthcheck, initialises the Raft
   backend (if first run), unseals, enables the `secret/` (KV v2),
   `auth/approle/`, and `transit/` mounts, writes the two ACL
   policies from `infra/openbao/policies/*.hcl`, and creates the
   two AppRoles — `openzync-app` and `openzync-worker` — with the
   policies bound to each.

2. **Phase 2 — System secrets (no DB).**  `openbao-init` writes a
   single combined secret at `system/config/data/system` containing
   all non-DB system config as a flat object: `REDIS_URL`,
   `SECRET_KEY`, `WEBHOOK_SIGNING_SECRET`, `PROMETHEUS_URL`,
   `OTEL_EXPORTER_OTLP_ENDPOINT`, log level, rate limits, feature
   flags.  The `DATABASE_URL` key is intentionally omitted — it
   doesn't exist yet.  This is the single seed point for all
   non-DB system config; future per-org config (ADR-005+) builds
   on this same mount.

3. **Phase 3 — AppRole credentials for the Agent sidecars.**  The
   api and worker AppRole `role_id` and `secret_id` values are
   written to four files in a shared docker volume
   (`openbao-init-data`): `/bao-init/api-role_id`,
   `/bao-init/api-secret_id`, `/bao-init/worker-role_id`,
   `/bao-init/worker-secret_id`.  Each file is `chmod 0600`.  The
   `openbao-agent-*` sidecars (Phase 8) read these at startup.
   The credentials never appear in `.env` and are never printed
   to logs.

4. **Phase 4 — Postgres starts.**  The `postgres` service starts
   (image: `pgvector/pgvector:pg15`, matching the RAG workload
   and pgvector extension requirements).  The superuser password
   is read from `POSTGRES_PASSWORD` in the deployment environment
   if present (allows CI / external-managed DBs); otherwise a
   random password is generated via `openssl rand -base64 32` and
   held in container memory only.  This password is discarded in
   Phase 5 and is never written to disk or to OpenBao.

5. **Phase 5 — DB users and least-privilege roles.**  The
   `postgres-init` sidecar connects as the superuser, creates the
   `openzync` database, and creates two application roles with
   auto-generated passwords: `openzync_migrator` (owns the schema;
   DDL — used ONLY by Alembic) and `openzync_app` (CRUD on
   existing objects — used by api and worker at runtime).
   `GRANT` statements and `ALTER DEFAULT PRIVILEGES` are applied
   so any future table created by `openzync_migrator` is
   automatically readable and writable by `openzync_app`.  All
   passwords are generated with `openssl rand -base64 32`.  The
   migrator and app passwords are written to
   `/bao-init/db-creds.json` (mode 0600).  The superuser password
   — whether inherited from `POSTGRES_PASSWORD` or freshly
   generated — is held in memory and discarded after Phase 5
   completes.

6. **Phase 6 — Migrations.**  The `postgres-migrate` service runs
   `alembic upgrade head` as the `openzync_migrator` user.  It
   uses the api image with a `command: ["alembic", "upgrade",
   "head"]` override — no separate Dockerfile, no separate
   dependency surface, no drift between "migrations" and
   "runtime" Python.  Migrations are idempotent and resumable
   via Alembic's standard `alembic_version` table.

7. **Phase 7 — DB credentials into OpenBao.**  The
   `openbao-write-db` sidecar reads `db-creds.json`, constructs
   the `DATABASE_URL`
   (`postgresql+asyncpg://openzync_migrator:<pw>@postgres:5432/openzync`),
   reads the existing `system` secret from OpenBao, MERGES in the
   new `DATABASE_URL` key, and writes the merged result back.
   Idempotency is enforced by a `db-creds-written` marker file in
   the shared volume — a re-run does not rotate the password.

8. **Phase 8 — api + worker start with Agent sidecars.**  Each
   service has an OpenBao Agent sidecar (`openbao-agent-api` and
   `openbao-agent-worker`) that authenticates to OpenBao via
   AppRole using the role_id + secret_id files from Phase 3.
   The Agent continuously renders the `system` secret to a tmpfs
   at `/openbao/agent/system.env` as a flat env file (KEY=VALUE
   per line).  Auto-token-rotation is enabled (default 5-minute
   refresh, no service interruption).  The api and worker
   entrypoint scripts (`entrypoint_api.sh`, `entrypoint_worker.sh`)
   `wait-for-file` on `system.env`, `source` it, and then `exec`
   uvicorn / ARQ respectively.  No secret ever appears on the
   command line or in process env outside the sourced env file.

The end result: `docker compose up -d` waits for the full chain
(OpenBao → Postgres → migrations → DB-cred-injection → Agent
render), and the api + worker only become ready after `system.env`
is in place.  No manual `make migrate`, no manual credential
copy-paste, no `OZ_DATABASE_URL` to remember.

---

## Consequences

### Positive

- **One command, one result.**  `docker compose up -d` produces a
  fully working OpenZync stack with no operator intervention
  between phases.
- **No secrets in `.env`.**  The DB password, AppRole `secret_id`s,
  and system signing keys all live in OpenBao.  `.env` is reduced
  to `BAO_STATIC_SEAL_KEY` (required) and `POSTGRES_PASSWORD`
  (optional) — both are deployment-environment concerns, not
  application secrets.
- **Agent auto-rotates tokens.**  The OpenBao Agent sidecar
  refreshes its AppRole token every 5 minutes.  No long-lived
  tokens sit in container memory for the lifetime of a pod.
- **tmpfs-mounted secrets.**  The rendered `system.env` lives on
  tmpfs (`/openbao/agent`) — never touches the host filesystem
  and is wiped on container restart.
- **Least-privilege DB roles.**  The api/worker connect as
  `openzync_app` (CRUD only); only Alembic connects as
  `openzync_migrator`.  A compromised api/worker process cannot
  `DROP TABLE users`, even via SQL injection.
- **Idempotent re-runs.**  `openbao-init`, `postgres-init`, and
  `openbao-write-db` all use marker files to avoid re-seeding or
  rotating credentials on every `docker compose up`.

### Negative

- **Slower initial boot.**  The eight-phase chain takes ~60s
  end-to-end on a fresh stack (OpenBao ~5s, Postgres ~10s,
  migrations ~5–30s, DB-cred-injection ~2s, Agent render ~3s).
  Healthcheck-based `depends_on` is the primary mitigation.
- **Password rotation invalidates in-flight connections.**  The
  `init_postgres.sh` script rotates the migrator and app
  passwords on every re-run that doesn't find the
  `db-creds-written` marker.  Mitigation: SQLAlchemy pool with
  1-hour `pool_recycle` plus `restart: unless-stopped`.  For v1
  the recommended workflow is `docker compose down` before `up`;
  rolling deploys are out of scope.
- **OpenBao Agent memory footprint.**  Each Agent sidecar adds
  ~30 MB.  For a 2-service stack (api + worker) this is ~60 MB
  total — acceptable for the security benefit, but worth noting
  for resource-constrained deployments.
- **Agent template rendering is pull-based.**  The Agent renders
  the `system` secret every 5 minutes (configurable via
  `template_config` in the Agent's `.hcl`).  Any manual edit via
  `bao kv put secret/system/system` will be REVERTED.  Edit via
  the `openbao-init` sidecar's seed script, or
  `docker compose restart openbao-agent-*` to force a re-render.

### Operational

- The `db-creds.json` and AppRole files live in the
  `openbao-init-data` docker volume.  If that volume is destroyed
  (e.g. `docker volume rm openzync_openbao-init-data`), the
  AppRole credentials and current DB passwords are LOST.
  Recovery: re-run `openbao-init` to regenerate AppRole
  `secret_id`s (the AppRole definitions persist in OpenBao's
  Raft storage), re-run `init_postgres.sh` to regenerate the DB
  users, then re-run `openbao-write-db` to push the new
  `DATABASE_URL` back into OpenBao.  The OpenBao Raft storage
  and `openbao-init-data` volume are the two pieces of state to
  back up for disaster recovery.

---

## Alternatives Considered

### A. Keep the current `OZ_DATABASE_URL`-from-env approach

Rejected.  It requires the operator to know the database URL before
deployment (chicken-and-egg with docker-compose), and the DB
password sits in `.env` alongside the OpenBao bootstrap credentials
— directly violating the spirit of ADR-003.  It also keeps the
"copy-paste the AppRole credentials into `.env`" workflow, the
single most common source of dev-time setup bugs in the current
stack.

### B. Use a single combined `openzync` user with broad DDL + CRUD

Rejected.  Violates principle of least-privilege — the api and
worker do not need DDL rights at runtime.  A SQL-injection bug or
malicious dependency should not be able to `DROP TABLE users` (or
`DROP SCHEMA public CASCADE`).  Splitting migrator vs app is ~20
extra lines of SQL for a meaningful security boundary.

### C. Use a single `openzync` user, but run migrations as the postgres superuser

Rejected.  The superuser password would need to persist somewhere
(`.env` or OpenBao) — putting us right back in the original
problem, and the superuser blast radius is the entire Postgres
instance (every database, every role), not just the `openzync`
database.  A leaked superuser password is a full Postgres
compromise, not a per-app compromise.

### D. Use the OpenBao Database secrets engine for dynamic short-lived credentials

Considered advanced; rejected for v1.  The Database engine generates
short-lived credentials on-demand (default 24h TTL).  However:
(1) our actual password change frequency is roughly "per release" —
longer than the 24h TTL — so the engine's rotation cadence is
SLOWER than our real change frequency, and the extra operational
complexity (lease management, lease revocation on user delete,
`revoke` on shutdown) isn't justified for v1; (2) the engine
requires a Postgres role-creation role to be granted to the OpenBao
server — more privilege to delegate than the static-user pattern
requires, and a privilege-escalation target if the OpenBao server
itself is compromised; (3) dynamic DB credentials interact poorly
with Alembic's `alembic_version` table ownership model — Alembic
assumes a stable connection identity for the migration role.
Re-evaluate in ADR-005 once the v1 stack is stable.

---

## References

- ADR-003: OpenBao as Sole Source of Truth — Zero Fallback
- `infra/openbao/agent/api.hcl`, `worker.hcl` — OpenBao Agent
  configuration (Phase 8)
- `infra/openbao/policies/api.hcl`, `worker.hcl` — ACL policies
  (Phase 1)
- `scripts/init_openbao.sh` — Phases 1–3: OpenBao init, system
  secrets, AppRole file emission
- `scripts/init_postgres.sh` — Phase 5: DB user / role creation
- `scripts/write_db_to_openbao.sh` — Phase 7: DB cred injection
- `scripts/entrypoint_api.sh`, `scripts/entrypoint_worker.sh` —
  Phase 8: wait for `system.env`, source it, `exec` the process
- OpenBao Agent documentation:
  https://github.com/openbao/openbao/blob/main/website/content/docs/agent/index.md

---

## Migration Path

Done in this single release:

1. `scripts/init_postgres.sh` and `scripts/write_db_to_openbao.sh`
   added (Phases 5, 7).
2. `infra/openbao/agent/api.hcl`, `worker.hcl` and the matching
   `entrypoint_*.sh` scripts added (Phase 8).
3. `infra/openbao/policies/api.hcl`, `worker.hcl` added (Phase 1).
4. docker-compose stack rewired with
   `depends_on: condition: service_healthy` for the eight-phase
   chain.
5. `.env.example` reduced to `BAO_STATIC_SEAL_KEY` (required) and
   `POSTGRES_PASSWORD` (optional, CI-only).
6. `make migrate` removed; migrations run inside the stack as
   `postgres-migrate`.

No rollback path is provided — the manual `OZ_DATABASE_URL` and
`make migrate` paths are deleted from the codebase.  To reverse,
restore the relevant files from version control history, re-seed
the OpenBao `system` secret with the old env-var values, and run
`alembic upgrade head` manually against the existing Postgres.
