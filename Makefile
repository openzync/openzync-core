# ──────────────────────────────────────────────────────────────────────────────
# OpenZync — Common development commands
# ──────────────────────────────────────────────────────────────────────────────
# Usage:  make <target> [ARGS=...]
#
# Examples:
#   make dev              # Start the API server
#   make test             # Run unit tests only
#   make test-all         # Run all tests (unit + integration + security)
#   make test ARGS="-k exceptions"   # Run only exception-related tests
#   make lint             # Ruff check + format
#   make migrate          # Apply pending Alembic migrations
#   make migrate-new      # Auto-generate a new migration revision
#   make docker-up        # Start infrastructure containers
#   make docker-down      # Stop infrastructure containers
# ──────────────────────────────────────────────────────────────────────────────

.PHONY: dev openbao-dev install lint test test-all test-coverage test-coverage-ci test-coverage-report test-coverage-html migrate migrate-new docker-up docker-down docs-install docs-build docs-watch docs-clean docs-apidoc clean

# ── Variables ─────────────────────────────────────────────────────────────────

PORT ?= 8000
PYTHON ?= python3
PIP ?= pip3

# ── Development server ────────────────────────────────────────────────────────

# Brings up persistent dev OpenBao (auto-bootstraps + syncs .env), then
# starts the API server with the bootstrap credentials loaded.
dev: openbao-dev
	@set -a && source .env && set +a && uvicorn services.api.asgi:app --reload --port $(PORT)

# Dev OpenBao — persistent (raft + static seal), idempotent bootstrap,
# writes fresh AppRole credentials into .env. Safe to re-run.
openbao-dev:
	@bash scripts/dev_openbao.sh

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
# Run the LongMemEval benchmark (requires live OpenZync instance + OpenRouter key).
# Options:  make benchmark ARGS="--benchmark-limit=10 --baseline --reranker"
benchmark:
	.venv/bin/python -m pytest tests/benchmarks/ --run-benchmark -v $(ARGS)

# ── Database ──────────────────────────────────────────────────────────────────

migrate:
	alembic upgrade head

migrate-check:
	alembic check

migrate-new:
	@read -p "Migration name: " name; alembic revision --autogenerate -m "$$name"

migrate-downgrade:
	alembic downgrade -1

# ── Docker ────────────────────────────────────────────────────────────────────

docker-up:
	docker compose -f infra/docker-compose.backend.yml up -d

docker-down:
	docker compose -f infra/docker-compose.backend.yml down

docker-logs:
	docker compose -f infra/docker-compose.backend.yml logs -f

docker-reset:
	docker compose -f infra/docker-compose.backend.yml down -v
	docker compose -f infra/docker-compose.backend.yml up -d

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

# ── Housekeeping ──────────────────────────────────────────────────────────────

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete
	rm -rf .pytest_cache .coverage coverage.xml htmlcov .mypy_cache .ruff_cache
