.PHONY: all format format-check lint typecheck test tests integration_tests help run dev dev-ui tunnel web build-dashboard desktop install-desktop install-checkout swagger platform-api platform-webhook platform-harness platform-deps platform-migrate platform-up platform-down platform-build platform-logs platform-ps

# Default target executed when no arguments are given to make.
all: help

######################
# DEVELOPMENT
######################

dev:
	uv run langgraph dev --no-browser --port 2024 --n-jobs-per-worker 10

# UI development in one terminal: Vite (`make web`) and the backend fronting it, so
# http://localhost:2024 hot-reloads without a build or any cross-origin setup. The two
# run side by side under -j2; Ctrl-C stops both. Command-line variables reach both recipes.
dev-ui:
	$(MAKE) --no-print-directory -j2 web dev DASHBOARD_DEV_SERVER_URL=http://localhost:3000 TURBO_UI=stream

web:
	pnpm run dev

# Public URL for GitHub and Slack webhooks while developing (docs/DEVELOPMENT.md, step 3).
# ngrok's free plan includes one static domain: NGROK_DOMAIN=<name>.ngrok-free.dev. The policy
# file exposes only /webhooks/*; langgraph dev has no auth, so the rest of the API stays local.
# Another tunnel is fine only if it enforces the same /webhooks/* allowlist (or a filtering proxy does).
tunnel:
	@test -n "$(NGROK_DOMAIN)" || { echo 'Set NGROK_DOMAIN=<your-domain>.ngrok-free.dev (claim it under Domains at https://dashboard.ngrok.com)' >&2; exit 1; }
	ngrok http 2024 --url https://$(NGROK_DOMAIN) --traffic-policy-file examples/ngrok/webhooks-only.yml

# Build the dashboard into ui/.output/public; `make dev` then serves it at /.
# With a LangGraph http.mount_prefix, pass DASHBOARD_BASE_PATH=<prefix>/ so the
# build's asset URLs and router match where the server mounts it.
build-dashboard:
	pnpm install --frozen-lockfile --filter alephat-dashboard...
	pnpm --filter alephat-dashboard run build

run:
	uv run uvicorn agent.webapp:app --reload --port 8000

swagger:
	uv run python -c 'import json; from pathlib import Path; from agent.webapp import app; Path("swagger.json").write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n", encoding="utf-8")'

desktop:
	pnpm run dev:desktop

install-desktop:
	@test -z "$$(git status --porcelain)" || { echo 'Commit or stash repository changes first.' >&2; exit 1; }
	@git switch main
	@git pull --ff-only origin main
	@./scripts/install_desktop.sh

install-checkout:
	@./scripts/install_desktop.sh

# Multi-service platform (see docs/PLATFORM.md)
# Prefer Compose v2 plugin; fall back to docker-compose binary.
COMPOSE := $(shell docker compose version >/dev/null 2>&1 && echo "docker compose" || echo "docker-compose")
COMPOSE_CMD := $(COMPOSE) -f deploy/docker-compose.yml --env-file deploy/.env

platform-deps:
	@test -f deploy/.env || cp deploy/.env.example deploy/.env
	$(COMPOSE_CMD) up -d postgres nats

platform-migrate:
	$(COMPOSE_CMD) run --rm migrate

# Full stack in Docker (api + webhook + harness + postgres + nats). No KEDA.
platform-build:
	@test -f deploy/.env || cp deploy/.env.example deploy/.env
	$(COMPOSE_CMD) build

platform-up:
	@test -f deploy/.env || cp deploy/.env.example deploy/.env
	$(COMPOSE_CMD) up --build -d
	@echo ""
	@echo "API     http://localhost:$${API_HOST_PORT:-8080}"
	@echo "UI      http://localhost:$${UI_HOST_PORT:-3000}/platform"
	@echo "Webhook http://localhost:$${WEBHOOK_HOST_PORT:-8081}"
	@echo "NATS    nats://localhost:$${NATS_HOST_PORT:-4222}  (monitor :$${NATS_MONITOR_HOST_PORT:-8222})"
	@echo "Harness is a separate worker container consuming JetStream TASKS."
	@echo "Scale workers: $(COMPOSE_CMD) up -d --scale harness=3"

platform-down:
	$(COMPOSE_CMD) down

platform-logs:
	$(COMPOSE_CMD) logs -f api webhook harness ui

platform-ps:
	$(COMPOSE_CMD) ps

# Host processes (use platform-deps for postgres/nats first)
platform-api:
	uv run uvicorn alephat_platform.api.app:app --reload --port 8080

platform-webhook:
	uv run uvicorn alephat_platform.webhook.app:app --reload --port 8081

platform-harness:
	uv run python -m alephat_platform.harness.worker

install:
	uv sync --extra dev

######################
# TESTING
######################

TEST_FILE ?= tests/

test tests:
	@if [ -d "$(TEST_FILE)" ] || [ -f "$(TEST_FILE)" ]; then \
		uv run pytest -vvv $(TEST_FILE); \
	else \
		echo "Skipping tests: path not found: $(TEST_FILE)"; \
	fi

integration_tests:
	@if [ -d "tests/integration_tests/" ] || [ -f "tests/integration_tests/" ]; then \
		uv run pytest -vvv tests/integration_tests/; \
	else \
		echo "Skipping integration tests: path not found: tests/integration_tests/"; \
	fi

######################
# LINTING AND FORMATTING
######################

PYTHON_FILES=.

lint:
	uv run ruff check $(PYTHON_FILES)
	uv run ruff format $(PYTHON_FILES) --diff

format:
	uv run ruff format $(PYTHON_FILES)
	uv run ruff check --fix $(PYTHON_FILES)

format-check:
	uv run ruff format $(PYTHON_FILES) --check

typecheck:
	uv run ty check agent alephat_platform tests

######################
# HELP
######################

help:
	@echo '----'
	@echo 'platform-deps                - start postgres + nats only'
	@echo 'platform-build               - build api/webhook/harness images'
	@echo 'platform-up                  - build+run full stack (no KEDA)'
	@echo 'platform-down                - stop compose stack'
	@echo 'platform-logs / platform-ps  - logs / status'
	@echo 'platform-api                 - host API on :8080 (needs platform-deps)'
	@echo 'platform-webhook             - host webhooks on :8081'
	@echo 'platform-harness             - host harness worker (picks up NATS jobs)'
	@echo 'dev                          - run LangGraph dev server'
	@echo 'dev-ui                       - Vite dev server plus the LangGraph dev server fronting it (UI hot reload on :2024)'
	@echo 'web                          - run the dashboard web server'
	@echo 'tunnel                       - ngrok tunnel to :2024 on NGROK_DOMAIN, webhooks only (any other tunnel works too)'
	@echo 'run                          - run webhook server'
	@echo 'swagger                      - regenerate swagger.json from the backend routes'
	@echo 'desktop                      - run the Electron desktop app (backend must be running)'
	@echo 'install-desktop              - install or update Alephat Desktop on macOS'
	@echo 'install-checkout             - install the current checkout of Alephat Desktop on macOS'
	@echo 'install                      - install dependencies (incl. dev extras)'
	@echo 'format                       - run code formatters'
	@echo 'lint                         - run linters'
	@echo 'typecheck                    - run ty on agent/ and tests/'
	@echo 'test                         - run unit tests'
	@echo 'integration_tests            - run integration tests'
	@echo '----'
