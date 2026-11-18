# ──────────────────────────────────────────────────────────────────────────────
# OpenZync — Common development commands
# ──────────────────────────────────────────────────────────────────────────────
# Usage:  make <target> [ARGS=...]
#
# Examples:
#   make dev              # Start the API server
#   make worker           # Start the ARQ worker (high + low queues)
#   make test             # Run unit tests only
#   make test-all         # Run all tests (unit + integration + security)
#   make test ARGS="-k exceptions"   # Run only exception-related tests
#   make lint             # Ruff check + format
#   make migrate          # Apply pending Alembic migrations
#   make migrate-new      # Auto-generate a new migration revision
#   make docker-up        # Start infrastructure containers
#   make docker-down      # Stop infrastructure containers
# ──────────────────────────────────────────────────────────────────────────────

.PHONY: dev deps worker install lint test test-all test-coverage test-coverage-ci test-coverage-report test-coverage-html migrate migrate-new docker-up docker-up-local docker-down docker-down-local docker-logs docker-logs-local docker-reset docker-reset-local docs-install docs-build docs-watch docs-clean docs-apidoc changelog-check changelog-build clean

# ── Variables ─────────────────────────────────────────────────────────────────

# Recipes use bashisms (`source`, `read -p`). make defaults to /bin/sh, which on
# Arch is bash in POSIX mode — there `source .env` searches $PATH, not the cwd.
SHELL := /bin/bash
PORT ?= 8000
PYTHON ?= python3
PIP ?= pip3
VERSION ?= 0.0.0

# ── Development servers ───────────────────────────────────────────────────────

# One-time (idempotent) bring-up of postgres+redis+falkordb+mailpit+openbao,
# OpenBao bootstrap and .env resync. Run this once; `dev` and `worker` assume
# it already ran. It re-mints AppRole secret_ids every run, so don't chain it.
deps:
	@bash scripts/dev_preflight.sh

dev:
	# Bind 0.0.0.0 so the Prometheus container can scrape host.docker.internal:8000 (uvicorn defaults to 127.0.0.1).
	@set -a && source .env && set +a && uvicorn services.api.asgi:app --reload --host 0.0.0.0 --port $(PORT) --reload

# Host-run ARQ worker. Same entrypoint the container execs (scripts/entrypoint_worker.sh
# only adds waiting for the OpenBao Agent sidecar to render /run/secrets/system.env).
# Holds :8081 (health) and :9095 (Prometheus) — do not run alongside the compose worker.
worker:
	@set -a && source .env && set +a && $(PYTHON) -m services.worker.worker

# ── Installation ──────────────────────────────────────────────────────────────

install:
	$(PIP) install -e ".[dev]"
	pre-commit install

# ── Linting ───────────────────────────────────────────────────────────────────

lint:
	ruff check . --output-format=concise
	ruff format --check .

lint-fix:
	ruff check . --fix --output-format=concise
	ruff format .

# ── Testing ───────────────────────────────────────────────────────────────────

test:
	pytest tests/unit/ -v $(ARGS)

test-all:
	pytest tests/ -v $(ARGS)

test-coverage:  ## Run ALL unit tests with coverage across all source directories
	pytest tests/unit/ -v \
		--cov=core --cov=routers --cov=services --cov=repositories \
		--cov=middleware --cov=dependencies --cov=workers --cov=packages \
		--cov=schemas --cov=utils \
		--cov-report=term --cov-report=html \
		--cov-fail-under=74 $(ARGS)

test-coverage-ci:  ## CI coverage job (unit + integration combined)
	pytest tests/unit/ tests/integration/ -v --timeout=60 \
		--cov=core --cov=routers --cov=services --cov=repositories \
		--cov=middleware --cov=dependencies --cov=workers --cov=packages \
		--cov=schemas --cov=utils \
		--cov-report=term --cov-report=xml \
		--cov-fail-under=75 $(ARGS)
		# Note: integration tests may have lower coverage due to testcontainers overhead

test-coverage-report:  ## Show coverage report (no run)
	coverage report

test-coverage-html:  ## Open HTML coverage report
	coverage html && open htmlcov/index.html

test-integration:
	pytest tests/integration/ -v --timeout=60 $(ARGS)

# ── Benchmarks ─────────────────────────────────────────────────────────────────
# Run the LongMemEval benchmark (requires live OpenZync instance + LLM API key).
# Runs are checkpointed after every question — an interrupted run auto-resumes
# from the newest matching manifest in benchmarks/results/.in_progress/.
# Query-only by default (reuses pre-ingested project data); pass --ingest to permit ingestion.
# Options:  make benchmark ARGS="--benchmark-limit=10 --baseline --reranker"
#           --fresh (ignore checkpoints, start over)
#           --resume PATH (resume one explicit manifest)
#           --workers N (judge N questions concurrently, default 1)
benchmark:
	.venv/bin/python -m benchmarks $(ARGS)

# ── Database ──────────────────────────────────────────────────────────────────

migrate:
	./scripts/migrate.sh upgrade head

migrate-check:
	./scripts/migrate.sh check

migrate-new:
	@read -p "Migration name: " name; ./scripts/migrate.sh revision --autogenerate -m "$$name"

migrate-downgrade:
	./scripts/migrate.sh downgrade -1

# ── Docker ────────────────────────────────────────────────────────────────────

docker-up:
	docker compose --env-file .env -f infra/docker-compose.backend.yml up -d --build

docker-up-local:
	docker compose --env-file .env -f infra/docker-compose.backend.yml --profile local-db up -d --build

docker-down:
	docker compose --env-file .env -f infra/docker-compose.backend.yml down

docker-down-local:
	docker compose --env-file .env -f infra/docker-compose.backend.yml --profile local-db down

# Option A (local-db) needs --profile local-db to include profiled services; use docker-logs-local for that.
docker-logs:
	docker compose --env-file .env -f infra/docker-compose.backend.yml logs -f

docker-logs-local:
	docker compose --env-file .env -f infra/docker-compose.backend.yml --profile local-db logs -f

docker-reset:
	docker compose --env-file .env -f infra/docker-compose.backend.yml down -v
	docker compose --env-file .env -f infra/docker-compose.backend.yml up -d --build

docker-reset-local:
	docker compose --env-file .env -f infra/docker-compose.backend.yml --profile local-db down -v
	docker compose --env-file .env -f infra/docker-compose.backend.yml --profile local-db up -d --build

# ── Documentation ─────────────────────────────────────────────────────────────

docs-install:
	$(PIP) install -e ".[doc]"

docs-build:
	sphinx-build -b html docs/ docs/_build/html

docs-watch:
	sphinx-autobuild docs/ docs/_build/html --port 8600

docs-clean:
	rm -rf docs/_build/

docs-apidoc:
	sphinx-apidoc -o docs/api/ \
	  core/ routers/ models/ schemas/ services/ repositories/ \
	  middleware/ dependencies/ workers/ utils/ packages/ \
	  --force --module-first

# ── Changelog (towncrier) ─────────────────────────────────────────────────────

changelog-check:
	@base=$$(git remote show origin 2>/dev/null | sed -n 's/.*HEAD branch: //p'); \
	if [ -z "$$base" ]; then base=main; fi; \
	if git rev-parse --verify origin/$$base >/dev/null 2>&1; then ref=origin/$$base; \
	elif git rev-parse --verify origin/main >/dev/null 2>&1; then ref=origin/main; \
	else ref=origin/master; fi; \
	echo "Checking fragments against $$ref..."; \
	towncrier check --compare-with $$ref --config pyproject.toml

changelog-build:
	towncrier build --version "$(VERSION)" --yes --config pyproject.toml

# ── Housekeeping ──────────────────────────────────────────────────────────────

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete
	rm -rf .pytest_cache .coverage coverage.xml htmlcov .mypy_cache .ruff_cache
