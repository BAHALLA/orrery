.PHONY: help install test eval lint type-check ty fmt infra-up infra-down infra-reset \
       docker-build docker-demo docker-down \
       docs-serve docs-build docs-deploy \
       run-docker run-docker-cli \
       run-kafka-health run-kafka-health-cli \
       run-k8s run-k8s-cli \
       run-observability run-observability-cli \
       run-elasticsearch run-elasticsearch-cli \
       run-assistant run-assistant-cli run-assistant-persistent \
       run-journal run-journal-cli run-journal-persistent \
       run-slack-bot run-slack-bot-socket \
       run-google-chat run-google-chat-pubsub

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-24s\033[0m %s\n", $$1, $$2}'

# ── Setup ──────────────────────────────────────────────

install: ## Install all workspace packages
	uv sync

test: ## Run all tests (excludes evals)
	uv run pytest -v

eval: ## Run agent evaluation tests (requires LLM access)
	uv run pytest -m eval -v

lint: ## Run linter checks (ruff check + format check)
	uv run ruff check .
	uv run ruff format --check .

type-check: ## Run type checks (ty)
	uv run ty check \
		--extra-search-path core \
		--extra-search-path agents/docker-agent \
		--extra-search-path agents/kafka-health \
		--extra-search-path agents/k8s-health \
		--extra-search-path agents/observability \
		--extra-search-path agents/elasticsearch \
		--extra-search-path agents/orrery-assistant \
		--extra-search-path agents/ops-journal \
		--extra-search-path agents/slack-bot \
		--extra-search-path agents/google-chat-bot \
		.

ty: type-check ## Alias for type-check


fmt: ## Auto-fix lint and format issues
	uv run ruff check --fix .
	uv run ruff format .

infra-up: ## Start shared infrastructure (Kafka, Prometheus, Loki, Alertmanager)
	docker compose up -d

infra-down: ## Stop shared infrastructure
	docker compose down

infra-reset: ## Stop infrastructure and wipe volumes
	docker compose down -v

# ── Documentation ──────────────────────────────────────

docs-serve: ## Serve documentation locally
	DISABLE_MKDOCS_2_WARNING=true uv run mkdocs serve

docs-build: ## Build documentation site
	DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build

docs-deploy: ## Deploy documentation to GitHub Pages
	DISABLE_MKDOCS_2_WARNING=true uv run mkdocs gh-deploy --force

# ── Docker ────────────────────────────────────────────

docker-build: ## Build the agent Docker image
	docker compose build orrery-assistant

docker-demo: ## Start full demo (infra + orrery-assistant web UI on :8000)
	docker compose --profile demo up -d --build

docker-down: ## Stop all services (infra + agents)
	docker compose --profile demo down

# ── docker-agent ──────────────────────────────────────

run-docker: ## Launch docker-agent in ADK Dev UI
	cd agents/docker-agent && uv run adk web

run-docker-cli: ## Run docker-agent in terminal
	cd agents/docker-agent && uv run adk run docker_agent

# ── kafka-health-agent ─────────────────────────────────

run-kafka-health: ## Launch kafka-health-agent in ADK Dev UI
	cd agents/kafka-health && uv run adk web

run-kafka-health-cli: ## Run kafka-health-agent in terminal
	cd agents/kafka-health && uv run adk run kafka_health_agent

# ── k8s-health-agent ──────────────────────────────────

run-k8s: ## Launch k8s-health-agent in ADK Dev UI
	cd agents/k8s-health && uv run adk web

run-k8s-cli: ## Run k8s-health-agent in terminal
	cd agents/k8s-health && uv run adk run k8s_health_agent

# ── observability-agent ────────────────────────────────

run-observability: ## Launch observability-agent in ADK Dev UI
	cd agents/observability && uv run adk web

run-observability-cli: ## Run observability-agent in terminal
	cd agents/observability && uv run adk run observability_agent

# ── elasticsearch-agent ────────────────────────────────

run-elasticsearch: ## Launch elasticsearch-agent in ADK Dev UI
	cd agents/elasticsearch && uv run adk web

run-elasticsearch-cli: ## Run elasticsearch-agent in terminal
	cd agents/elasticsearch && uv run adk run elasticsearch_agent

# ── orrery-assistant ───────────────────────────────────

run-assistant: ## Launch orrery-assistant in ADK Dev UI
	cd agents/orrery-assistant && ENABLE_METRICS_SERVER=true uv run adk web

run-assistant-cli: ## Run orrery-assistant in terminal
	cd agents/orrery-assistant && ENABLE_METRICS_SERVER=true uv run adk run orrery_assistant

run-assistant-persistent: ## Run orrery-assistant with SQLite persistence
	cd agents/orrery-assistant && ENABLE_METRICS_SERVER=true uv run python run_persistent.py

# ── ops-journal ────────────────────────────────────────

run-journal: ## Launch ops-journal in ADK Dev UI (in-memory state)
	cd agents/ops-journal && uv run adk web

run-journal-cli: ## Run ops-journal in terminal (in-memory state)
	cd agents/ops-journal && uv run adk run ops_journal_agent

run-journal-persistent: ## Run ops-journal with SQLite persistence
	cd agents/ops-journal && uv run python run_persistent.py

# ── slack-bot ─────────────────────────────────────────

run-slack-bot: ## Run the Slack bot (FastAPI + slack-bolt on :3000)
	cd agents/slack-bot && uv run uvicorn slack_bot.app:api --host 0.0.0.0 --port 3000

run-slack-bot-socket: ## Run the Slack bot in Socket Mode (no public URL needed)
	cd agents/slack-bot && uv run python -m slack_bot

# ── google-chat-bot ──────────────────────────────────

run-google-chat: ## Run the Google Chat bot (FastAPI on :3001)
	cd agents/google-chat-bot && uv run uvicorn google_chat_bot.app:api --host 0.0.0.0 --port 3001

run-google-chat-pubsub: ## Run the Google Chat bot in Pub/Sub mode (private GKE friendly)
	cd agents/google-chat-bot && uv run python -m google_chat_bot.pubsub_worker
