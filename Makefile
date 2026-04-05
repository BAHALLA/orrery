.PHONY: help install test eval lint fmt infra-up infra-down infra-reset \
       docker-build docker-demo docker-down \
       run-kafka-health run-kafka-health-cli \
       run-k8s run-k8s-cli \
       run-observability run-observability-cli \
       run-devops run-devops-cli run-devops-persistent \
       run-journal run-journal-cli run-journal-persistent \
       run-slack-bot run-slack-bot-socket

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

fmt: ## Auto-fix lint and format issues
	uv run ruff check --fix .
	uv run ruff format .

infra-up: ## Start shared infrastructure (Kafka, Prometheus, Loki, Alertmanager)
	docker compose up -d

infra-down: ## Stop shared infrastructure
	docker compose down

infra-reset: ## Stop infrastructure and wipe volumes
	docker compose down -v

# ── Docker ────────────────────────────────────────────

docker-build: ## Build the agent Docker image
	docker compose build devops-assistant

docker-demo: ## Start full demo (infra + devops-assistant web UI on :8000)
	docker compose --profile demo up -d --build

docker-down: ## Stop all services (infra + agents)
	docker compose --profile demo down

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

# ── devops-assistant ───────────────────────────────────

run-devops: ## Launch devops-assistant in ADK Dev UI
	cd agents/devops-assistant && ENABLE_METRICS_SERVER=true uv run adk web

run-devops-cli: ## Run devops-assistant in terminal
	cd agents/devops-assistant && ENABLE_METRICS_SERVER=true uv run adk run devops_assistant

run-devops-persistent: ## Run devops-assistant with SQLite persistence
	cd agents/devops-assistant && ENABLE_METRICS_SERVER=true uv run python run_persistent.py

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
