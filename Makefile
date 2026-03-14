.PHONY: help install infra-up infra-down infra-reset \
       run-kafka-health run-kafka-health-cli \
       run-devops run-devops-cli run-devops-persistent \
       run-journal run-journal-cli run-journal-persistent

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-24s\033[0m %s\n", $$1, $$2}'

# ── Setup ──────────────────────────────────────────────

install: ## Install all workspace packages
	uv sync

infra-up: ## Start shared infrastructure (Kafka, Zookeeper, Kafka UI)
	docker compose up -d

infra-down: ## Stop shared infrastructure
	docker compose down

infra-reset: ## Stop infrastructure and wipe volumes (fixes cluster.id mismatch)
	docker compose down -v

# ── kafka-health-agent ─────────────────────────────────

run-kafka-health: ## Launch kafka-health-agent in ADK Dev UI
	cd agents/kafka-health && uv run adk web

run-kafka-health-cli: ## Run kafka-health-agent in terminal
	cd agents/kafka-health && uv run adk run kafka_health_agent

# ── devops-assistant ───────────────────────────────────

run-devops: ## Launch devops-assistant in ADK Dev UI
	cd agents/devops-assistant && uv run adk web

run-devops-cli: ## Run devops-assistant in terminal
	cd agents/devops-assistant && uv run adk run devops_assistant

run-devops-persistent: ## Run devops-assistant with SQLite persistence
	cd agents/devops-assistant && uv run python run_persistent.py

# ── ops-journal ────────────────────────────────────────

run-journal: ## Launch ops-journal in ADK Dev UI (in-memory state)
	cd agents/ops-journal && uv run adk web

run-journal-cli: ## Run ops-journal in terminal (in-memory state)
	cd agents/ops-journal && uv run adk run ops_journal_agent

run-journal-persistent: ## Run ops-journal with SQLite persistence
	cd agents/ops-journal && uv run python run_persistent.py
