# Common commands. `make` (or `make help`) lists them all.

.DEFAULT_GOAL := help
.PHONY: help install up down clean logs demo e2e test lint format typecheck audit check

E2E_BASE_URL ?= http://localhost:8000
E2E_API_KEY ?= dev-key

help: ## Show this list
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies (Python 3.12 via uv)
	uv sync

# --- Docker stack ---------------------------------------------------------------

up: ## Start PostgreSQL, migrations, API and worker
	docker compose up -d --build --wait

down: ## Stop the stack (data is kept)
	docker compose down

clean: ## Stop the stack and delete the database volume
	docker compose down -v

logs: ## Follow API and worker logs
	docker compose logs -f api worker

demo: ## Walk through the full flow against the running stack
	./scripts/demo.sh

e2e: ## End-to-end tests against the running stack
	E2E_BASE_URL=$(E2E_BASE_URL) E2E_API_KEY=$(E2E_API_KEY) uv run pytest -m e2e -v

# --- Quality checks (CI runs the same) ------------------------------------------

test: ## Unit and integration tests with coverage (SQLite)
	uv run pytest --cov

lint: ## Lint and formatting check
	uv run ruff check .
	uv run ruff format --check .

format: ## Format the code and apply safe lint fixes
	uv run ruff format .
	uv run ruff check --fix .

typecheck: ## Type check with mypy
	uv run mypy

audit: ## Scan runtime dependencies for known vulnerabilities
	@requirements=$$(mktemp) && \
	uv export --frozen --no-dev --no-emit-project --format requirements.txt --quiet -o $$requirements && \
	uv run pip-audit -r $$requirements --require-hashes --disable-pip --progress-spinner off; \
	status=$$?; rm -f $$requirements; exit $$status

check: lint typecheck test audit ## Everything CI checks, except PostgreSQL and e2e
