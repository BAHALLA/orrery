# Contributing to Orrery

Thanks for your interest! Orrery is a platform of autonomous DevOps/SRE agents
built on [Google ADK](https://google.github.io/adk-docs/) and managed as a
[uv workspace](https://docs.astral.sh/uv/concepts/workspaces/). The most valuable
contributions are **new specialist agents**, **new tools on existing agents**, and
**hardening of the shared core**.

## Getting Started

1. **Fork and clone** the repository.
2. **Install everything** (all workspace packages + dev tools):
   ```bash
   make install   # uv sync --all-extras, plus npm ci for the web console
   ```
   Node is optional — without `npm` the console is skipped and everything else
   still works.
3. **Configure your LLM** — copy the root env file and set a provider + key:
   ```bash
   cp .env.example .env   # set MODEL_PROVIDER / MODEL_NAME + the matching API key
   ```
4. **Start infrastructure** (only if you're working on agents that need Kafka,
   Postgres, Prometheus, …):
   ```bash
   make up
   ```
5. **Run the orchestrator** to verify your setup:
   ```bash
   make run-dev     # ADK Dev UI at http://localhost:8000
   ```
   Agents are composed by `orrery-assistant`, so you generally run the
   orchestrator rather than each agent standalone. There is one target per
   surface (`run-dev`, `run-cli`, `run-api`, `run-web`, `run-slack`, `run-chat`,
   `run-triage`); `make help` lists them all.

## Project Structure

```
orrery/
├── core/                    # Shared library (orrery-core): agent factory,
│   └── tests/               #   plugins, RBAC, guardrails, validation, resilience
├── agents/                  # Each agent is its own workspace package
│   ├── kafka-health/        #   Kafka + Strimzi
│   ├── k8s-health/          #   Kubernetes + operators
│   ├── elasticsearch/       #   Elasticsearch + ECK
│   ├── observability/       #   Prometheus / Loki / Alertmanager
│   ├── docker-agent/        #   Docker + Compose
│   ├── ops-journal/         #   State-management demo
│   ├── orrery-assistant/    #   Root orchestrator + triage/remediation workflow
│   ├── slack-bot/           #   Slack transport
│   └── google-chat-bot/     #   Google Chat transport
│       └── tests/           #   Every package keeps its tests beside it
├── web/                     # React + TypeScript web console
└── docs/                    # MkDocs site (guides, ADRs, AEP roadmap)
```

See [`core/README.md`](core/README.md) for the shared-library API (agent factory,
guardrails, RBAC, validation, plugins).

## How to Contribute

### Adding a New Agent

The most impactful contribution. Follow the
[Adding a New Agent](docs/adding-an-agent.md) walkthrough. Key points:

- Create a package under `agents/` and register it as in
  [step 5](docs/adding-an-agent.md#5-register-in-workspace): the root and
  `orrery-assistant` dependencies, `registry._WORKSPACE_DISTRIBUTIONS`, and an
  `orrery.specialists` entry point.
- Use `create_agent()` from `orrery_core` — don't reinvent the factory.
- Define every tool as `async def`; offload blocking I/O with
  `asyncio.to_thread()` (or `create_subprocess_exec`).
- Separate tools (`tools.py`) from agent wiring (`agent.py`).
- Mark mutating tools `@confirm("reason")` and destructive tools
  `@destructive("reason")` — RBAC and the confirmation gate are inferred from these.
- **No callback wiring needed** — cross-cutting concerns (RBAC, guardrails, audit,
  metrics, PII redaction, …) are applied globally by plugins on the Runner.
- Add tests under `agents/<your-agent>/tests/` and a `README.md` in the package.
- No edit to `orrery-assistant`: the entry point is how the chat root and the
  triage workflow discover the agent.

### Improving Existing Agents & Core

- New tools on an existing agent, or sharper agent instructions (they directly
  drive tool-selection quality — see [Agent Evaluations](docs/evals.md)).
- New guardrail strategies, plugins, validators, or resilience improvements in
  `core/`. A change that benefits multiple agents belongs in the core, not copied.

### Proposing a Larger Change

Substantial features are tracked as **Agent Enhancement Proposals (AEP)** under
[`docs/enhancements/`](docs/enhancements/README.md). If you're planning something
big (a new subsystem, a cross-cutting behavior), open an AEP first so the design
can be discussed before code — follow the structure of an existing one.

## Development Workflow

1. **Branch off `main`:**
   ```bash
   git checkout -b feat/my-new-agent
   ```
2. **Make your changes** following the patterns in existing agents.
3. **Run the checks** before pushing:
   ```bash
   make fmt    # auto-fix lint + formatting, Python and web
   make check  # the whole gate: lint + types + Python tests + web — mirrors CI
   make eval   # OPTIONAL: agent eval scenarios — needs LLM credentials
   ```
   `make check` is the one to run before pushing. Its parts are also available
   individually (`lint`, `type-check`, `test`, `test-web`) when you want a
   faster loop.
4. **Update the docs and CHANGELOG.** Add a `[Unreleased]` entry to
   [`CHANGELOG.md`](CHANGELOG.md) (this project keeps it current per change) and
   touch any affected page under `docs/`.
5. **Open a pull request** with:
   - A [Conventional Commit](https://www.conventionalcommits.org/) title
     (`feat(kafka): …`, `fix(security): …`, `docs: …`) — the history uses them.
   - A clear description of what changed and why.
   - Tests for new tools, and a screenshot from the ADK Dev UI if it's user-facing.

## Testing Guidelines

- **Tests live beside each package** — add a `tests/` directory in your agent.
- **All tool tests are async** — `@pytest.mark.asyncio` + `async def`.
- **Mock external dependencies** — no test may require a live Kafka broker, K8s
  cluster, Docker daemon, or HTTP backend. Mock at the client-getter layer
  (`@patch("my_agent.tools._get_client")`); use `AsyncMock` for async helpers.
- **Mock *every* client the agent can touch**, including operator clients
  (Strimzi/ECK/Kubernetes) — not just the primary one. An unmocked operator call
  reaches live infrastructure and makes tests non-deterministic (see
  [Agent Evaluations](docs/evals.md)).
- **Cover success and error paths** — every tool needs at least one success test
  and one error/exception test.
- **Test input validation** — assert invalid inputs return `{"status": "error", …}`
  (empty strings, oversized values, path traversal, bad patterns).
- **Verify guardrails** — for a `@confirm`/`@destructive` tool, assert the
  `_guardrail_level` attribute is set.
- **Reuse fixtures** — if a tool needs ADK's `ToolContext`, add a `conftest.py`
  with a `FakeToolContext` (see `core/tests/conftest.py`).

## Code Style

- **Ruff** for linting and formatting (line length 100, target py314); CI runs
  both `ruff check` and `ruff format --check`. Run `make fmt` before committing.
- Tools are `async def` functions returning a `dict` with a `status` field.
- Use type hints; keep each tool focused on one operation.
- **Validate every input** at the top of each tool, using the walrus pattern:
  ```python
  from orrery_core.security.validation import validate_string, validate_positive_int


  async def my_tool(name: str, count: int = 10) -> dict:
      if err := validate_string(name, "name", max_len=200):
          return err
      if err := validate_positive_int(count, "count", max_value=1000):
          return err
      ...
  ```
- Follow the existing patterns — `kafka-health` is the reference implementation.

## Agent Design Guidelines

- **Read-only by default.** Mark anything that mutates state `@confirm`/`@destructive`.
- **Instructions are the product.** Clear, specific instructions drive good
  tool-selection; ambiguous ones make behavior non-deterministic and evals flaky.
  Tell the agent which tool family owns which question and to call only what the
  question needs.
- **Descriptions are routing signals.** A sub-agent's `description` decides when
  the orchestrator delegates to it.
- **Return structured data**, not formatted strings — let the model format the reply.

## Reporting Issues

Open a GitHub issue with what you were trying to do, what happened instead, steps
to reproduce, and any agent logs or ADK Dev UI screenshots. For **security**
issues, follow [`SECURITY.md`](SECURITY.md) instead — do not open a public issue.

## License

By contributing, you agree your contributions are licensed under the
[MIT License](LICENSE).
