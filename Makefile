# wekruit-matching — Docker-first developer ergonomics.
#
# These targets are thin aliases around `docker compose`. They exist so the
# common operations (scrape-once, scrape-daemon, logs, db-shell) are one
# muscle-memory command and don't rely on remembering which container is
# named what.
#
# Usage:
#   make help              # list targets
#   make up                # start db + app (FastAPI on 127.0.0.1:8001)
#   make scrape-once       # run one daily-pipeline cycle, exit
#   make scrape-daemon     # launchd-style: scrape every 24h forever
#   make logs              # tail app logs
#   make db-shell          # psql into the embedded Postgres
#   make app-shell         # exec into the app container
#   make down              # stop everything (keeps the PG volume)
#   make nuke              # stop and DROP the PG volume — destroys data
#
# All targets implicitly call `docker compose` from the repo root, so they
# work from a fresh clone without any extra setup beyond `cp .env.docker.example .env`.

DC := docker compose

.PHONY: help up down logs scrape-once scrape-daemon app-shell db-shell rebuild status nuke

help:
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) 2>/dev/null | awk -F ':.*?##' '{printf "  %-18s %s\n", $$1, $$2}'

up: ## Start db + app in the background
	$(DC) up -d

down: ## Stop containers (preserves Postgres volume)
	$(DC) down

logs: ## Tail app logs
	$(DC) logs -f app

status: ## Show service health
	$(DC) ps

scrape-once: ## Run one daily-pipeline cycle, exit when done
	$(DC) run --rm app uv run python -m wekruit_matching.pipeline.daily

scrape-daemon: ## Loop scrape-once every 24h forever (Ctrl-C to stop)
	$(DC) run --rm app bash -c 'while true; do uv run python -m wekruit_matching.pipeline.daily; echo "[scrape-daemon] sleeping 24h"; sleep 86400; done'

app-shell: ## Open a shell in the app container
	$(DC) exec app bash

db-shell: ## psql into the embedded Postgres
	$(DC) exec db psql -U wekruit -d wekruit_matching

rebuild: ## Force-rebuild the app image
	$(DC) build --no-cache app
	$(DC) up -d

nuke: ## Stop and DROP the PG volume — destroys all local pipeline data
	$(DC) down -v

# ---------------------------------------------------------------------------
# Test gate — mirrors .github/workflows/ci.yml so you can run it locally.
# ---------------------------------------------------------------------------
# `make ci` reproduces the CI job 1:1 (migrate + round-trip + full pytest) and
# assumes DATABASE_URL + WEKRUIT_TEST_DB already point at a THROWAWAY *_test DB.
# It runs no scrape / LLM stage — same as CI.

.PHONY: test test-integration ci

test: ## Run the fast unit suite (no DB / no integration tests)
	uv run pytest -q -m "not integration"

test-integration: ## Run the full suite incl. DB integration tests (needs WEKRUIT_TEST_DB=1 + a *_test DB)
	uv run pytest -q

ci: ## Reproduce the CI gate locally: migrate + alembic round-trip + full pytest
	uv run alembic upgrade head
	uv run alembic downgrade -1
	uv run alembic upgrade head
	uv run pytest -q
