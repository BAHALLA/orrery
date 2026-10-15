.PHONY: help \
        install check fmt test test-web test-cov eval lint type-check ty clean \
        lock lock-check knowledge-sync \
        run-dev run-cli run-api run-web run-slack run-chat run-triage \
        up down reset logs ps \
        dev-token dev-token-reset \
        docs docs-build docs-deploy \
        web-build _console _dev-jwt-secret

# One target per job. Variants are flags, not extra targets:
#   make run-cli PERSIST=1     make run-api SSO=1
#   make run-slack MODE=socket make run-chat MODE=pubsub
#   make dev-token ROLE=viewer make up PROFILES=tracing

help: ## Show this help
	@echo "Orrery — make <target>"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "  Flags: SSO=1 PERSIST=1 MODE=socket|pubsub ROLE=viewer|operator|admin"
	@echo "         PROFILES=\"tracing sso elastic\"  FORCE=1"

WEB_DIR        := web
WEB_STATIC_DIR := core/orrery_core/serving/static
ASSISTANT_DIR  := agents/orrery-assistant

# ── Local dev auth ─────────────────────────────────────
# A persistent JWT secret for local-only work, stored outside the repo so it
# can't be committed. `run-api` and `dev-token` read the same file, so a token
# minted in one terminal always validates against the server in another.
DEV_JWT_SECRET_FILE := $(HOME)/.cache/orrery/jwt-secret
DEV_JWT_AUDIENCE    := orrery-local
DEV_JWT_ISSUER      := https://dev.local

# Local Keycloak (the `sso` compose profile). The console and the API must agree
# on the issuer string exactly — Keycloak signs `iss` with the URL the browser
# used, so "localhost" here and "127.0.0.1" there fails verification.
SSO_ISSUER    := http://localhost:8081/realms/orrery
SSO_CLIENT_ID := orrery-console

# ── Setup & quality gate ───────────────────────────────

install: ## Install everything (Python workspace + web console)
	uv sync --all-extras
	@if command -v npm >/dev/null 2>&1; then \
		cd $(WEB_DIR) && npm ci; \
	else \
		echo "▶ npm not found — skipped the web console (Python-only setup)"; \
	fi

check: lock-check lint lint-workflows type-check test test-web ## Full gate: lockfile + lint + types + Python tests + web (mirrors CI)

# `uv sync` silently *rewrites* a stale lock, so nothing ever failed on drift —
# a Dependabot PR that edited pyproject.toml alone merged green and left the lock
# recording the old constraint. The lock is tracked precisely so Docker builds
# are reproducible, which a lock that disagrees with the manifest is not.
lock-check: ## Fail if uv.lock has drifted from pyproject.toml
	@uv lock --check || { \
		echo ""; \
		echo "▶ uv.lock is out of sync with pyproject.toml. Run 'make lock' and commit the result."; \
		exit 1; \
	}

lock: ## Regenerate uv.lock after editing pyproject.toml
	uv lock

fmt: ## Auto-fix formatting and lint, Python and web
	uv run ruff check --fix .
	uv run ruff format .
	@command -v npm >/dev/null 2>&1 && (cd $(WEB_DIR) && npm run format) || true

test: ## Run Python tests (excludes evals)
	uv run pytest -v

# Deliberately not part of `check` and deliberately not gated on a threshold.
# A number picked today would be arbitrary, and a failing coverage gate teaches
# people to write tests that touch lines rather than tests that assert things.
# This exists so the number is *visible* — a test count says how much was run,
# not how much is covered.
test-cov: ## Run Python tests with a coverage report (htmlcov/ for the detail)
	uv run pytest --cov --cov-report=term-missing:skip-covered --cov-report=html -q
	@echo "▶ Full report: htmlcov/index.html"

test-web: ## Run the web console gate (lint + format + types + tests + build)
	@if command -v npm >/dev/null 2>&1; then \
		cd $(WEB_DIR) && npm run check; \
	else \
		echo "▶ npm not found — SKIPPING the web gate. CI still runs it."; \
	fi

eval: ## Run agent evaluation scenarios (requires LLM access)
	uv run pytest -m eval -v

lint: ## Lint Python (ruff check + format check)
	uv run ruff check .
	uv run ruff format --check .

# Pinned for the same reason CI pins its actions: a linter that changes between
# runs changes verdicts. Bump both here; CI reads them from this target.
ACTIONLINT_VERSION ?= 1.7.12.25
SHELLCHECK_VERSION ?= 0.11.0.1
ZIZMOR_VERSION ?= 1.31.0

lint-workflows: ## Lint GitHub Actions workflows (actionlint + zizmor security audit)
	@# shellcheck is installed alongside, not taken from the host: actionlint
	@# silently skips its `run:` checks when shellcheck is absent, which is how a
	@# local run passed while CI (whose runner ships shellcheck) failed.
	uvx --with shellcheck-py==$(SHELLCHECK_VERSION) --from actionlint-py==$(ACTIONLINT_VERSION) \
		actionlint .github/workflows/*.yml
	uvx zizmor==$(ZIZMOR_VERSION) --offline --min-severity low .github/workflows/

# Derived from the workspace members (core + every agents/* with a pyproject),
# so a newly added agent is picked up automatically — no manual edit needed.
TY_SEARCH_PATHS := $(addprefix --extra-search-path ,core $(patsubst %/,%,$(dir $(wildcard agents/*/pyproject.toml))))

type-check: ## Type-check Python (ty)
	uv run ty check $(TY_SEARCH_PATHS) .

ty: type-check ## Alias for type-check

# Trees `clean` must never walk into. node_modules is the important one: plenty
# of npm packages ship their compiled output in a directory literally named
# `build` (pretty-format, jwt-decode, …), so a bare `find . -name build -exec
# rm -rf` silently guts them and the web tests then fail with "Cannot find
# module …/build/index.js" long after the fact.
CLEAN_PRUNE := -type d \( -name .venv -o -name node_modules -o -name .git \) -prune -o

clean: ## Remove caches and build artifacts (keeps .venv and node_modules)
	@echo "▶ Cleaning build artifacts and caches…"
	@find . $(CLEAN_PRUNE) -type d \
		\( -name '__pycache__' -o -name '*.egg-info' -o -name 'build' -o -name '.adk' \) \
		-print0 | xargs -0 --no-run-if-empty rm -rf
	@find . $(CLEAN_PRUNE) -type f -name '*.py[co]' -print0 | xargs -0 --no-run-if-empty rm -f
	rm -rf .pytest_cache .ruff_cache .hypothesis .mypy_cache .coverage htmlcov dist site
	@echo "▶ Clean. (.venv and node_modules preserved.)"

# ── Infrastructure ─────────────────────────────────────
# Profiles `up` starts. The app profiles (demo, slack) are excluded on purpose:
# they run the agent itself in Docker on the same ports as `make run-api` and
# `make run-slack`, so starting them by default would collide with local runs.
# Pick your own set with:  make up PROFILES="tracing"
PROFILES ?= tracing sso elastic
COMPOSE_PROFILES := $(addprefix --profile ,$(PROFILES))

up: ## Start all containers (Kafka, Postgres + pgAdmin, observability, Keycloak, Elasticsearch)
	docker compose $(COMPOSE_PROFILES) up -d
	@if echo "$(PROFILES)" | grep -qw sso; then \
		echo "▶ Waiting for the Keycloak realm to import…"; \
		ready=0; for i in $$(seq 1 60); do \
			if curl -sf $(SSO_ISSUER)/.well-known/openid-configuration >/dev/null 2>&1; then ready=1; break; fi; \
			sleep 2; \
		done; \
		if [ $$ready -eq 0 ]; then \
			echo "✗ Keycloak realm did not come up. Import errors are the usual cause:"; \
			docker logs keycloak 2>&1 | grep -i "ERROR" | tail -5; \
			exit 1; \
		fi; \
		echo "▶ Keycloak ready at $(SSO_ISSUER) — users viewer/operator/admin (password = username)"; \
	fi
	@$(MAKE) --no-print-directory ps

down: ## Stop all containers (volumes are kept)
	docker compose $(COMPOSE_PROFILES) down

reset: ## Stop all containers AND delete their volumes (destroys local data)
	@echo "▶ This deletes these volumes and everything in them:"
	@docker volume ls --format '{{.Name}}' | grep "^$$(basename $$PWD)_" | sed 's/^/    /' || true
	@if [ "$(FORCE)" != "1" ]; then \
		printf "▶ Type 'yes' to continue: "; read ans; [ "$$ans" = "yes" ] || { echo "Aborted."; exit 1; }; \
	fi
	docker compose $(COMPOSE_PROFILES) down -v

ps: ## Show running containers
	@docker compose $(COMPOSE_PROFILES) ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}'

logs: ## Tail container logs (SERVICE=kafka to narrow)
	docker compose $(COMPOSE_PROFILES) logs -f --tail=100 $(SERVICE)

# ── Run ────────────────────────────────────────────────
# The orchestrator composes every specialist agent (kafka / k8s / observability
# / elasticsearch / docker / ops-journal), so run it rather than each agent
# standalone. One target per surface.

run-dev: ## Agent in the ADK Dev UI (no auth, developer tooling)
	cd $(ASSISTANT_DIR) && ENABLE_METRICS_SERVER=true uv run adk web

run-cli: ## Agent in the terminal (PERSIST=1 for a persistent store)
ifeq ($(PERSIST),1)
	cd $(ASSISTANT_DIR) && ENABLE_METRICS_SERVER=true uv run python run_persistent.py
else
	cd $(ASSISTANT_DIR) && ENABLE_METRICS_SERVER=true uv run adk run orrery_assistant
endif

run-triage: ## Run the deterministic triage Workflow once (batch / scheduled)
	cd $(ASSISTANT_DIR) && uv run python run_triage.py

# Indexing is a build-time action, never lazy at request time — a first query
# that silently triggers a full crawl is an outage waiting for its moment.
# ROOT is repeatable via `ROOT="docs/runbooks docs/adr"`; PRUNE=0 keeps
# documents no source produced this run.
knowledge-sync: ## Index the docs corpus into ORRERY_KNOWLEDGE_BACKEND
	uv run python scripts/knowledge_sync.py \
		$(foreach r,$(ROOT),--root $(r)) \
		$(if $(filter 0,$(PRUNE)),--no-prune,) \
		$(if $(filter 0,$(GIT)),--no-git,)

run-web: ## Web console dev server with HMR (Vite :5173, proxies the API on :8000)
	cd $(WEB_DIR) && npm run dev

# `run-api` serves the console at / as well, so it is the one command for
# "the product". SSO=1 swaps the pasted-token gate for Keycloak (needs `make up`
# with the sso profile), which changes both the console build and how the server
# verifies tokens — hence the paired blocks below.
ifeq ($(SSO),1)
WEB_BUILD_ENV := VITE_OIDC_ISSUER=$(SSO_ISSUER) \
                 VITE_OIDC_CLIENT_ID=$(SSO_CLIENT_ID) \
                 VITE_OIDC_ROLE_CLAIM=realm_access.roles
API_AUTH_ENV  := JWT_ALGORITHM=RS256 \
                 JWT_JWKS_URL=$(SSO_ISSUER)/protocol/openid-connect/certs \
                 JWT_AUDIENCE=$(SSO_CLIENT_ID) \
                 JWT_ISSUER=$(SSO_ISSUER) \
                 JWT_ROLE_CLAIM=realm_access.roles
else
WEB_BUILD_ENV :=
API_AUTH_ENV  := JWT_ALGORITHM=HS256 \
                 JWT_SECRET=$$(cat $(DEV_JWT_SECRET_FILE)) \
                 JWT_AUDIENCE=$(DEV_JWT_AUDIENCE) \
                 JWT_ISSUER=$(DEV_JWT_ISSUER)
endif

run-api: _dev-jwt-secret _console ## API front door + web console on :8000 (SSO=1 for Keycloak)
ifeq ($(SSO),1)
	@curl -sf $(SSO_ISSUER)/.well-known/openid-configuration >/dev/null 2>&1 || \
		{ echo "✗ Keycloak unreachable at $(SSO_ISSUER). Run: make up PROFILES=sso"; exit 1; }
	@echo "▶ http://localhost:8000 — sign in as viewer / operator / admin"
else
	@echo "▶ http://localhost:8000 — mint a token in another terminal: make dev-token"
endif
	cd $(ASSISTANT_DIR) && \
		AUTH_ENABLED=true \
		$(API_AUTH_ENV) \
		ENABLE_METRICS_SERVER=true \
		ORRERY_WEB_CONSOLE_ENABLED=true \
		uv run uvicorn orrery_assistant.app:api --host 0.0.0.0 --port 8000

run-slack: ## Slack bot on :3000 (MODE=socket for Socket Mode, no public URL)
ifeq ($(MODE),socket)
	cd agents/slack-bot && uv run python -m slack_bot
else
	cd agents/slack-bot && uv run uvicorn slack_bot.app:api --host 0.0.0.0 --port 3000
endif

run-chat: ## Google Chat bot on :3001 (MODE=pubsub for private-GKE Pub/Sub mode)
ifeq ($(MODE),pubsub)
	cd agents/google-chat-bot && uv run python -m google_chat_bot.pubsub_worker
else
	cd agents/google-chat-bot && uv run uvicorn google_chat_bot.app:api --host 0.0.0.0 --port 3001
endif

# Build the console into the package so the API can serve it. Node is optional
# for Python-only contributors, so a missing npm warns instead of failing.
_console:
	@if command -v npm >/dev/null 2>&1; then \
		(cd $(WEB_DIR) && $(WEB_BUILD_ENV) npm run build) && \
		rm -rf $(WEB_STATIC_DIR) && mkdir -p $(WEB_STATIC_DIR) && \
		cp -R $(WEB_DIR)/dist/. $(WEB_STATIC_DIR)/ && \
		echo "▶ Console built into $(WEB_STATIC_DIR)"; \
	else \
		echo "▶ npm not found — serving whatever console bundle is already built"; \
	fi

web-build: _console ## Build the console into the package without starting the API

# ── Dev auth tokens ────────────────────────────────────

_dev-jwt-secret:
	@if [ ! -f $(DEV_JWT_SECRET_FILE) ]; then \
		mkdir -p $$(dirname $(DEV_JWT_SECRET_FILE)) && \
		openssl rand -hex 32 > $(DEV_JWT_SECRET_FILE) && \
		chmod 600 $(DEV_JWT_SECRET_FILE) && \
		echo "▶ Generated dev JWT secret at $(DEV_JWT_SECRET_FILE)"; \
	fi

dev-token: _dev-jwt-secret ## Mint a local JWT (ROLE=viewer|operator|admin, default admin)
	@JWT_AUDIENCE=$(DEV_JWT_AUDIENCE) \
	JWT_ISSUER=$(DEV_JWT_ISSUER) \
		uv run python scripts/dev_token.py \
			--role $(or $(ROLE),admin) \
			--secret-file $(DEV_JWT_SECRET_FILE)

dev-token-reset: ## Regenerate the dev JWT secret (invalidates existing tokens)
	@rm -f $(DEV_JWT_SECRET_FILE)
	@$(MAKE) --no-print-directory _dev-jwt-secret
	@echo "Secret file: $(DEV_JWT_SECRET_FILE)"
	@echo "Audience:    $(DEV_JWT_AUDIENCE)"
	@echo "Issuer:      $(DEV_JWT_ISSUER)"

# ── Documentation ──────────────────────────────────────

docs: ## Serve the documentation site locally
	DISABLE_MKDOCS_2_WARNING=true uv run mkdocs serve

docs-build: ## Build the documentation site
	DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build

docs-deploy: ## Deploy documentation to GitHub Pages
	DISABLE_MKDOCS_2_WARNING=true uv run mkdocs gh-deploy --force
