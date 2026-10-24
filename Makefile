# One target per job. Variants are flags, not extra targets:
#   make run-cli PERSIST=1     make run-api SSO=1
#   make run-slack MODE=socket make run-chat MODE=pubsub
#   make dev-token ROLE=viewer make up PROFILES=tracing

# Every target is phony (none builds a file of its name). Derived from the
# targets themselves, so a new one can't be forgotten here — the hand-kept list
# this replaced had already drifted.
.PHONY: $(shell sed -n 's/^\([a-z_][a-z0-9_-]*\):.*/\1/p' $(firstword $(MAKEFILE_LIST)))

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

# Node is optional for Python-only contributors: web steps skip (with a note)
# when npm is missing, instead of failing.
HAVE_NPM := $(shell command -v npm 2>/dev/null)
NO_NPM    = echo "▶ npm not found — skipped: $(1)"

# The assistant's run targets all share this prefix.
RUN := cd $(ASSISTANT_DIR) && ENABLE_METRICS_SERVER=true uv run

# Quiet MkDocs' banner about its 2.0 plans in every docs command.
export DISABLE_MKDOCS_2_WARNING := true

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
	@$(if $(HAVE_NPM),cd $(WEB_DIR) && npm ci,$(call NO_NPM,the web console))

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
	@$(if $(HAVE_NPM),cd $(WEB_DIR) && npm run format,$(call NO_NPM,web formatting))

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
	@$(if $(HAVE_NPM),cd $(WEB_DIR) && npm run check,$(call NO_NPM,the web gate — CI still runs it))

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
	uvx zizmor==$(ZIZMOR_VERSION) --offline --min-severity low .github/workflows/ .github/actions/

# Derived from the workspace members (core + every agents/* with a pyproject),
# so a newly added agent is picked up automatically — no manual edit needed.
TY_SEARCH_PATHS := $(addprefix --extra-search-path ,core $(patsubst %/,%,$(dir $(wildcard agents/*/pyproject.toml))))

type-check: ## Type-check Python (ty)
	uv run ty check $(TY_SEARCH_PATHS) .

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
COMPOSE  := docker compose $(addprefix --profile ,$(PROFILES))

up: ## Start all containers (Kafka, Postgres + pgAdmin, observability, Keycloak, Elasticsearch)
	$(COMPOSE) up -d
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
	$(COMPOSE) down

reset: ## Stop all containers AND delete their volumes (destroys local data)
	@echo "▶ This deletes these volumes and everything in them:"
	@docker volume ls --format '{{.Name}}' | grep "^$$(basename $$PWD)_" | sed 's/^/    /' || true
	@if [ "$(FORCE)" != "1" ]; then \
		printf "▶ Type 'yes' to continue: "; read ans; [ "$$ans" = "yes" ] || { echo "Aborted."; exit 1; }; \
	fi
	$(COMPOSE) down -v

ps: ## Show running containers
	@$(COMPOSE) ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}'

logs: ## Tail container logs (SERVICE=kafka to narrow)
	$(COMPOSE) logs -f --tail=100 $(SERVICE)

# ── Run ────────────────────────────────────────────────
# The orchestrator composes every specialist agent (kafka / k8s / observability
# / elasticsearch / docker / ops-journal), so run it rather than each agent
# standalone. One target per surface.

run-dev: ## Agent in the ADK Dev UI (no auth, developer tooling)
	$(RUN) adk web

run-cli: ## Agent in the terminal (PERSIST=1 for a persistent store)
	$(RUN) $(if $(filter 1,$(PERSIST)),python run_persistent.py,adk run orrery_assistant)

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
# with the sso profile). The console reads its OIDC settings at runtime from
# /console-config.js, so the same bundle serves both modes: SSO=1 only changes
# the server's environment.
ifeq ($(SSO),1)
API_AUTH_ENV  := ORRERY_CONSOLE_OIDC_ISSUER=$(SSO_ISSUER) \
                 ORRERY_CONSOLE_OIDC_CLIENT_ID=$(SSO_CLIENT_ID) \
                 JWT_ALGORITHM=RS256 \
                 JWT_JWKS_URL=$(SSO_ISSUER)/protocol/openid-connect/certs \
                 JWT_AUDIENCE=$(SSO_CLIENT_ID) \
                 JWT_ISSUER=$(SSO_ISSUER) \
                 JWT_ROLE_CLAIM=realm_access.roles
else
API_AUTH_ENV  := JWT_ALGORITHM=HS256 \
                 JWT_SECRET=$$(cat $(DEV_JWT_SECRET_FILE)) \
                 JWT_AUDIENCE=$(DEV_JWT_AUDIENCE) \
                 JWT_ISSUER=$(DEV_JWT_ISSUER)
endif

# Loopback by default: the agent runs with your kubeconfig, cluster and LLM
# credentials, so a laptop on a shared network should not serve it to the LAN.
# API_HOST=0.0.0.0 to reach it from another machine (a VM's host, a phone).
API_HOST ?= 127.0.0.1

run-api: _dev-jwt-secret web-build ## API front door + web console on :8000 (SSO=1 for Keycloak, API_HOST=0.0.0.0 for the LAN)
ifeq ($(SSO),1)
	@curl -sf $(SSO_ISSUER)/.well-known/openid-configuration >/dev/null 2>&1 || \
		{ echo "✗ Keycloak unreachable at $(SSO_ISSUER). Run: make up PROFILES=sso"; exit 1; }
	@echo "▶ http://localhost:8000 — sign in as viewer / operator / admin"
else
	@echo "▶ http://localhost:8000 — mint a token in another terminal: make dev-token"
endif
	$(RUN) env AUTH_ENABLED=true $(API_AUTH_ENV) ORRERY_WEB_CONSOLE_ENABLED=true \
		uvicorn orrery_assistant.app:api --host $(API_HOST) --port 8000

run-slack: ## Slack bot on :3000 (MODE=socket for Socket Mode, no public URL)
	cd agents/slack-bot && uv run $(if $(filter socket,$(MODE)),python -m slack_bot,uvicorn slack_bot.app:api --host 0.0.0.0 --port 3000)

run-chat: ## Google Chat bot on :3001 (MODE=pubsub for private-GKE Pub/Sub mode)
	cd agents/google-chat-bot && uv run $(if $(filter pubsub,$(MODE)),python -m google_chat_bot.pubsub_worker,uvicorn google_chat_bot.app:api --host 0.0.0.0 --port 3001)

web-build: ## Build the console into the package (run-api does this for you)
	@$(if $(HAVE_NPM),\
		cd $(WEB_DIR) && npm run build && cd - >/dev/null && \
		rm -rf $(WEB_STATIC_DIR) && mkdir -p $(WEB_STATIC_DIR) && \
		cp -R $(WEB_DIR)/dist/. $(WEB_STATIC_DIR)/ && \
		echo "▶ Console built into $(WEB_STATIC_DIR)",\
		$(call NO_NPM,the console build — serving whatever bundle is already built))

# ── Demo ───────────────────────────────────────────────
# A real incident on the local stack, for showing Orrery in a minute: a consumer
# group thousands of messages behind, because its worker is OOM-killed in a loop
# (scripts/demo/seed.py). Nothing is mocked — the agents find what is there, so
# this needs your LLM credentials in .env like any other run. Postgres is in the
# list because a typical .env points DATABASE_URL at it.
DEMO_SERVICES := postgres kafka kafka-exporter prometheus alertmanager

demo: ## A seeded incident (Kafka lag + an OOM-looping worker) and the console on :8000
	docker compose up -d $(DEMO_SERVICES)
	uv run python scripts/demo/seed.py
	@echo "▶ KafkaConsumerGroupLagHigh fires in Prometheus in ~30 s."
	@echo "▶ Open http://localhost:8000 and paste this operator token:"
	@$(MAKE) --no-print-directory dev-token ROLE=operator
	@echo "▶ Then ask: \"Why is the orders consumer lagging?\" — and ask it to fix what it finds."
	@echo "▶ Only the demo's own systems are on: Kubernetes and Elasticsearch would act on your kubeconfig and cluster."
	@ORRERY_SPECIALISTS=kafka,docker,observability,ops_journal $(MAKE) --no-print-directory run-api

demo-reset: ## Remove the demo's seeded faults (the topic, group and worker)
	uv run python scripts/demo/seed.py --reset

# The README GIF: the real console and server, with a scripted model so the
# recording is reproducible and free. Needs Google Chrome. The trap kills the
# server's children too: `uv run` doesn't forward a kill to the Python process.
demo-gif: web-build ## Re-record docs/images/console-demo.gif
	@set -e; \
	DATABASE_URL= OTEL_TRACING_ENABLED=false uv run python scripts/demo/scripted_server.py >/dev/null 2>&1 & \
	server=$$!; trap 'pkill -P $$server 2>/dev/null; kill $$server 2>/dev/null' EXIT; \
	for i in $$(seq 1 60); do curl -sf http://127.0.0.1:8100/healthz >/dev/null && break; sleep 0.5; done; \
	cd scripts/demo && uv run --with pillow --with playwright==1.55.0 \
		python record_gif.py --out ../../docs/images/console-demo.gif

# ── Dev auth tokens ────────────────────────────────────

_dev-jwt-secret:
	@if [ ! -f $(DEV_JWT_SECRET_FILE) ]; then \
		mkdir -p $$(dirname $(DEV_JWT_SECRET_FILE)) && \
		openssl rand -hex 32 > $(DEV_JWT_SECRET_FILE) && \
		chmod 600 $(DEV_JWT_SECRET_FILE) && \
		echo "▶ Generated dev JWT secret at $(DEV_JWT_SECRET_FILE)"; \
	fi

dev-token: _dev-jwt-secret ## Mint a local JWT (ROLE=viewer|operator|admin, default admin; CLIENT=<azp>)
	@JWT_AUDIENCE=$(DEV_JWT_AUDIENCE) \
	JWT_ISSUER=$(DEV_JWT_ISSUER) \
		uv run python scripts/dev_token.py \
			--role $(or $(ROLE),admin) $(if $(CLIENT),--client $(CLIENT),) \
			--secret-file $(DEV_JWT_SECRET_FILE)

dev-token-reset: ## Regenerate the dev JWT secret (invalidates existing tokens)
	@rm -f $(DEV_JWT_SECRET_FILE)
	@$(MAKE) --no-print-directory _dev-jwt-secret
	@echo "Secret file: $(DEV_JWT_SECRET_FILE)"
	@echo "Audience:    $(DEV_JWT_AUDIENCE)"
	@echo "Issuer:      $(DEV_JWT_ISSUER)"

# ── Documentation ──────────────────────────────────────
# Publishing is CI's job: the Docs workflow deploys GitHub Pages on every merge
# to main. (A local `mkdocs gh-deploy` would push a gh-pages branch that the
# Actions-based Pages deployment ignores — two publishers for one site.)

docs: ## Serve the documentation site locally
	uv run mkdocs serve

docs-build: ## Build the documentation site (strict, as CI does)
	uv run mkdocs build --strict
