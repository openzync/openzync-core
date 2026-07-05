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

.PHONY: dev install lint test test-all migrate migrate-new docker-up docker-down docs-install docs-build docs-watch docs-clean docs-apidoc clean

# ── Variables ─────────────────────────────────────────────────────────────────

PORT ?= 8000
PYTHON ?= python3
PIP ?= pip3

# ── Development server ────────────────────────────────────────────────────────

dev:
	uvicorn services.api.asgi:app --reload --port $(PORT)

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

test-coverage:
	pytest tests/unit/ -v --cov=core --cov=middleware --cov=dependencies --cov-report=term --cov-report=html

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
