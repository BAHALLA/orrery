# Contributing to AI Agents

Thanks for your interest in contributing! This project aims to build a collection of useful, autonomous DevOps/SRE agents using Google ADK.

## Getting Started

1. **Fork and clone** the repository
2. **Install dependencies**:
   ```bash
   make install
   ```
3. **Start infrastructure** (if working on agents that need Kafka, etc.):
   ```bash
   make infra-up
   ```
4. **Run an agent** to verify your setup:
   ```bash
   make run-kafka-health
   ```

## Project Structure

```
ai-agents/
├── core/                  # Shared library (ai-agents-core)
│   └── tests/             # Core tests (guardrails, audit, config)
├── agents/                # Each agent is a separate workspace package
│   ├── kafka-health/
│   │   └── tests/         # Kafka tools tests
│   ├── k8s-health/
│   │   └── tests/         # Kubernetes tools tests
│   ├── devops-assistant/
│   │   └── tests/         # Docker tools tests
│   └── ops-journal/
│       └── tests/         # Journal & state tools tests
└── docs/                  # Per-agent and core documentation
```

See [core/README.md](core/README.md) for the shared library API (agent factory, guardrails, audit, config).

## How to Contribute

### Adding a New Agent

This is the most impactful way to contribute. See the [Adding a New Agent](docs/adding-an-agent.md) guide for the step-by-step walkthrough. Key points:

- Create a new directory under `agents/`
- Use `create_agent()` from `ai_agents_core` — don't reinvent the factory
- Mark destructive tools with `@destructive("reason")`
- Separate tools (`tools.py`) from agent wiring (`agent.py`)
- Add tests in `agents/your-agent/tests/`
- Add a `README.md` in your agent package
- Add Makefile targets for running the agent

### Improving Existing Agents

- Add new tools to existing agents
- Improve agent instructions for better LLM behavior
- Add error handling for edge cases

### Improving the Core Library

- New guardrail strategies
- Better audit logging (e.g., structured logging backends)
- Config improvements
- Utility functions that benefit multiple agents

## Development Workflow

1. **Create a branch** for your work:
   ```bash
   git checkout -b feature/my-new-agent
   ```

2. **Make your changes** following the patterns in existing agents

3. **Add tests** for your tools in `agents/your-agent/tests/`:
   ```bash
   # Run all tests
   make test

   # Run tests for your agent only
   uv run pytest agents/your-agent/tests/ -v
   ```

4. **Test your changes manually**:
   ```bash
   make install
   cd agents/your-agent && uv run adk web
   ```

5. **Submit a pull request** with:
   - A clear description of what the agent/feature does
   - Screenshots from the ADK Dev UI if applicable
   - Tests for new tools
   - Updated docs and Makefile targets

## Testing Guidelines

- **Tests live next to each package** — add a `tests/` directory inside your agent package
- **Mock external dependencies** — no test should require a running Kafka broker, Kubernetes cluster, or Docker daemon
- **Use `@patch`** to mock API clients at the tool level (e.g., `@patch("my_agent.tools._get_client")`)
- **Test both success and error paths** — every tool should have at least one success test and one error/exception test
- **Verify guardrails** — if your tool uses `@confirm` or `@destructive`, add a test asserting the `_guardrail_level` attribute
- **Use conftest fixtures** — if your tools need ADK's `ToolContext`, add a `conftest.py` with a `FakeToolContext` class (see `core/tests/conftest.py` or `agents/ops-journal/tests/conftest.py` for examples)

## Code Style

- Tools are plain Python functions returning `dict` with a `status` field
- Use type hints
- Keep tools focused — one function per operation
- Follow existing patterns (look at `kafka-health` as the reference)

## Agent Design Guidelines

- **Tools should be read-only by default.** Mark anything that modifies state with `@destructive()`
- **Agent instructions matter.** Spend time crafting clear instructions — they directly affect how well the LLM uses your tools
- **Sub-agent descriptions are routing signals.** When composing agents into an orchestrator, the `description` field determines when the orchestrator delegates to your agent
- **Return structured data.** Tools should return dicts, not formatted strings — let the LLM format the response for the user

## Reporting Issues

Open an issue on GitHub with:
- What you were trying to do
- What happened instead
- Steps to reproduce
- Agent logs or ADK Dev UI screenshots if relevant

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
