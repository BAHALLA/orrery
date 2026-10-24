"""Studio runs (AEP-031 §2): one gateway per run, apart from the live coordinator.

Each run gets its own :class:`~orrery_core.serving.gateway.AgentGateway`:

- ``app_name="orrery-studio"``, ``session_service=InMemorySessionService()``
  (explicitly, so no durable store is ever picked up), ``memory_service=None``
  and ``verified_confirmation=True``;
- a fresh plugin list from ``default_plugins(autonomy_level="L2")``, so
  delegation, repeat and circuit-breaker state never mixes with chat traffic,
  and every tool, catalogue tools included, is held to reads. The daily token
  budget is the exception: its ledger is the process's shared one;
- the session id ``s-<run id>``, which ``is_machine_session()`` keeps out of
  long-term memory.

It never calls ``replace_root_agent()``, never writes the catalog and never
touches the chat gateway. :class:`StudioRunPlugin` (added to that run's
plugins only) emits the trace events and enforces the composition ledger.

Runs live in memory on the replica that started them, bounded by count and
age. Cancelling a run cancels its task: model and HTTP calls in flight are
cancelled and toolsets closed. A synchronous tool already running in a worker
thread cannot be interrupted; it finishes in the background, harmlessly at L2.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.sessions import InMemorySessionService
from google.genai import types

from ..a2a.remote import RemoteCards, RemoteExchange
from ..observability.audit import audit_event
from ..plugins import CALL_BUDGET_STATUS, ErrorHandlerPlugin, default_plugins
from ..plugins.delegation_guard_plugin import is_delegation
from ..reliability.resilience import ToolOutcome, classify_tool_outcome
from ..security.auth import AUTH_STATE_KEY, AuthContext
from ..security.identity import CallerIdentity
from ..security.principal import STUDIO_SESSION_PREFIX
from ..serving.gateway import AgentGateway, TurnTimedOut
from .compiler import CompiledComposition, compile_spec
from .config import StudioConfig
from .events import (
    ALWAYS_KEPT,
    MAX_EVENTS_PER_RUN,
    RESERVED_EVENTS,
    A2aMessage,
    BudgetHit,
    McpCall,
    NodeEnter,
    NodeExit,
    NodeOutcome,
    PartSummary,
    RunEnd,
    RunOutcome,
    RunStart,
    TokenCount,
    Totals,
    _Event,
    capture,
)
from .ledger import CompositionLedger, Exhausted
from .registry import StudioRegistry
from .spec import (
    CompositionSpec,
    SpecError,
    SpecInvalid,
    ValidationContext,
    parse_spec,
    pipeline_order,
    validate_spec,
)

logger = logging.getLogger("orrery.studio")

APP_NAME = "orrery-studio"

RunStatus = Literal["running", "succeeded", "failed", "cancelled", "budget_exhausted"]


class TooManyRuns(Exception):
    """A start refused because too many runs are in flight."""


class _BudgetStop(Exception):
    """Raised from the event hook to end a run whose ledger ran out."""


class _NodeFailed(Exception):
    """Raised from the event hook to end a run whose model call failed."""


@dataclass
class StudioRun:
    """One run: its spec, status and events. Read by the API, written by the run task."""

    run_id: str
    owner: str
    spec: CompositionSpec
    spec_digest: str
    input: str
    created_at: float = field(default_factory=time.time)
    status: RunStatus = "running"
    ended_at: float | None = None
    error: str | None = None
    totals: Totals | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    dropped: int = 0
    task: asyncio.Task[None] | None = None
    _changed: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def finished(self) -> bool:
        return self.status != "running"

    def emit(self, event: _Event) -> None:
        """Append *event* (numbered and stamped here), unless past the per-run cap."""
        kind = getattr(event, "type", "")
        if (
            len(self.events) >= MAX_EVENTS_PER_RUN - RESERVED_EVENTS and kind not in ALWAYS_KEPT
        ) or len(self.events) >= MAX_EVENTS_PER_RUN:
            self.dropped += 1
            return
        event.seq = len(self.events) + 1
        event.run_id = self.run_id
        event.ts = time.time()
        if isinstance(event, RunEnd):
            event.dropped_events = self.dropped
        self.events.append(event.model_dump(mode="json", exclude_none=True))
        self._notify()

    def _notify(self) -> None:
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    async def wait(self, timeout: float) -> None:
        """Until the next event or *timeout* seconds, whichever comes first."""
        changed = self._changed
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout):
                await changed.wait()

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "spec_name": self.spec.name,
            "spec_digest": self.spec_digest,
            "created_at": self.created_at,
            "ended_at": self.ended_at,
            "error": self.error,
            "totals": self.totals.model_dump(mode="json") if self.totals else None,
            "event_count": len(self.events),
            "dropped_events": self.dropped,
        }


class StudioRunPlugin(BasePlugin):
    """Trace events and the composition ledger for one run. Fresh per run."""

    def __init__(
        self,
        run: StudioRun,
        ledger: CompositionLedger,
        compiled: CompiledComposition,
        *,
        capture_content: bool,
    ) -> None:
        super().__init__(name="studio_run")
        self._run = run
        self._ledger = ledger
        self._compiled = compiled
        self._capture = capture_content
        self._entered: dict[str, float] = {}
        self._tokens: dict[str, TokenCount] = {}
        self._last_text: dict[str, str] = {}
        self._tool_started: dict[str, float] = {}
        #: ``(agent name, error type)`` of the first failed model call. The
        #: error handler answers it with an apology, which would otherwise pass
        #: for the node's output and let the run "succeed".
        self.failure: tuple[str, str] | None = None

    # ── nodes ──
    async def before_agent_callback(self, *, agent: Any, callback_context: Any) -> None:
        info = self._compiled.nodes.get(agent.name)
        if info is None:
            return None
        self._entered[agent.name] = time.monotonic()
        self._run.emit(NodeEnter(node_id=info.node_id, kind=info.kind, model=info.model))
        return None

    async def after_agent_callback(self, *, agent: Any, callback_context: Any) -> None:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            # ADK runs this hook while a cancel (or the deadline) unwinds the
            # node: leave it open, and the run's ``finally`` closes it with the
            # real outcome instead of "ok".
            return None
        failed = self.failure is not None and self.failure[0] == agent.name
        self._exit(agent.name, "failed" if failed else "ok")
        return None

    def _exit(self, agent_name: str, outcome: NodeOutcome) -> None:
        info = self._compiled.nodes.get(agent_name)
        started = self._entered.pop(agent_name, None)
        if info is None or started is None:
            return
        self._run.emit(
            NodeExit(
                node_id=info.node_id,
                kind=info.kind,
                model=info.model,
                tokens=self._tokens.get(agent_name, TokenCount()),
                elapsed_ms=int((time.monotonic() - started) * 1000),
                outcome=outcome,
                content=capture(self._last_text.get(agent_name)) if self._capture else None,
            )
        )

    def close_open_nodes(self, outcome: NodeOutcome) -> None:
        for name in list(self._entered):
            self._exit(name, outcome)

    def last_output(self) -> str | None:
        for agent_name in reversed(list(self._compiled.nodes)):
            if text := self._last_text.get(agent_name):
                return text
        return None

    # ── models ──
    async def before_model_callback(self, *, callback_context: Any, llm_request: Any) -> Any:
        if self._ledger.exhausted is None:
            return None
        # Spent: answer without buying more tokens; the run ends on the next event.
        return LlmResponse(
            content=types.Content(
                role="model", parts=[types.Part.from_text(text="The run's budget is spent.")]
            )
        )

    async def after_model_callback(self, *, callback_context: Any, llm_response: Any) -> None:
        if getattr(llm_response, "partial", None) is True:
            return None  # a streamed chunk repeats in the final response
        name = str(getattr(callback_context, "agent_name", "") or "")
        info = self._compiled.nodes.get(name)
        usage = getattr(llm_response, "usage_metadata", None)
        if usage is not None:
            prompt = int(getattr(usage, "prompt_token_count", 0) or 0)
            output = int(getattr(usage, "candidates_token_count", 0) or 0)
            total = int(getattr(usage, "total_token_count", 0) or 0) or prompt + output
            node = self._tokens.setdefault(name, TokenCount())
            node.input += prompt
            node.output += output
            node.total += total
            hit = self._ledger.charge_tokens(info.model if info else "", prompt, output, total)
            if hit is not None:
                self.emit_budget(hit)
        content = getattr(llm_response, "content", None)
        if content is not None and content.parts:
            text = "".join(p.text for p in content.parts if p.text and p.thought is not True)
            if text:
                self._last_text[name] = text
        return None

    async def on_model_error_callback(
        self, *, callback_context: Any, llm_request: Any, error: Exception
    ) -> None:
        if self.failure is None:
            name = str(getattr(callback_context, "agent_name", "") or "")
            self.failure = (name, type(error).__name__)
        return None  # ErrorHandlerPlugin still answers; the run ends on the next event

    # ── tools ──
    async def before_tool_callback(
        self, *, tool: Any, tool_args: dict[str, Any], tool_context: Any
    ) -> dict[str, Any] | None:
        if self._ledger.exhausted is None:
            hit = self._ledger.charge_tool(
                delegation=is_delegation(tool),
                remote=getattr(tool, "delegates_remotely", False) is True,
            )
            if hit is not None:
                self.emit_budget(hit)
        if self._ledger.exhausted is not None:
            return {
                "status": CALL_BUDGET_STATUS,
                "error": "This Studio run's budget is spent; the run is ending.",
            }
        if tool.name in self._compiled.mcp_tools:
            self._tool_started[_call_key(tool, tool_context)] = time.monotonic()
        return None

    async def after_tool_callback(
        self, *, tool: Any, tool_args: dict[str, Any], tool_context: Any, result: Any
    ) -> None:
        info = self._compiled.mcp_tools.get(tool.name)
        if info is None:
            return None
        started = self._tool_started.pop(_call_key(tool, tool_context), None)
        outcome = classify_tool_outcome(result)
        status = result.get("status") if isinstance(result, dict) else None
        self._run.emit(
            McpCall(
                node_id=info.node_id,
                entry=info.entry,
                tool=info.tool,
                classification=info.classification,
                outcome={
                    ToolOutcome.SUCCESS: "success",
                    ToolOutcome.FAILURE: "failure",
                    ToolOutcome.IGNORE: "refused",
                }[outcome],
                status=status if isinstance(status, str) else None,
                elapsed_ms=int((time.monotonic() - started) * 1000) if started else None,
            )
        )
        return None

    def emit_budget(self, hit: Exhausted) -> None:
        self._run.emit(BudgetHit(budget=hit.budget, limit=hit.limit, used=hit.used))


def _call_key(tool: Any, tool_context: Any) -> str:
    call_id = getattr(tool_context, "function_call_id", None)
    return str(call_id) if call_id else f"{tool.name}:{id(tool_context)}"


def _insert_before_error_handler(plugins: list[BasePlugin], plugin: BasePlugin) -> None:
    # ErrorHandlerPlugin must stay last (it answers tool errors).
    for i, existing in enumerate(plugins):
        if isinstance(existing, ErrorHandlerPlugin):
            plugins.insert(i, plugin)
            return
    plugins.append(plugin)


def studio_plugins() -> list[BasePlugin]:
    """A fresh plugin list for one run: chat's stack, at L2, without memory."""
    return default_plugins(enable_auth=True, autonomy_level="L2", enable_memory=False)


class StudioService:
    """Validates specs, starts runs and keeps them (per process, bounded)."""

    def __init__(
        self,
        *,
        config: StudioConfig,
        registry: StudioRegistry,
        cards: RemoteCards,
        catalogue: Callable[[], Mapping[str, Any]] | None = None,
        model_factory: Callable[[], Any] | None = None,
        plugin_factory: Callable[[], list[BasePlugin]] = studio_plugins,
        caller_identity: Callable[[AuthContext], CallerIdentity | None] | None = None,
    ) -> None:
        self.config = config
        self.registry = registry
        self.cards = cards
        self._catalogue = catalogue or dict
        self._model_factory = model_factory
        self._plugin_factory = plugin_factory
        self._caller_identity = caller_identity
        self._runs: OrderedDict[str, StudioRun] = OrderedDict()

    def catalogue(self) -> Mapping[str, Any]:
        return self._catalogue()

    def validation_context(self) -> ValidationContext:
        return ValidationContext(
            registry=self.registry, catalogue=self.catalogue(), ceilings=self.config.ceilings
        )

    def check(self, raw: Mapping[str, Any]) -> CompositionSpec:
        """*raw* parsed and validated. Raises :class:`SpecInvalid` with every problem."""
        spec = parse_spec(raw)
        if errors := validate_spec(spec, self.validation_context()):
            raise SpecInvalid(errors)
        return spec

    # ── runs ──
    def _prune(self) -> None:
        now = time.time()
        for run_id, run in list(self._runs.items()):
            if run.finished and now - (run.ended_at or now) > self.config.retention_seconds:
                del self._runs[run_id]
        while len(self._runs) > self.config.max_runs:
            oldest = next((r for r in self._runs.values() if r.finished), None)
            if oldest is None:
                break
            del self._runs[oldest.run_id]

    def running(self, owner: str | None = None) -> int:
        return sum(
            1 for r in self._runs.values() if not r.finished and (owner is None or r.owner == owner)
        )

    def start(self, spec: CompositionSpec, text: str, auth: AuthContext) -> StudioRun:
        if not text.strip():
            raise SpecInvalid([SpecError(("input",), "input must not be empty")])
        self._prune()
        if self.running(auth.subject) >= self.config.max_running_per_user:
            raise TooManyRuns(
                f"you already have {self.config.max_running_per_user} Studio runs in flight"
            )
        if self.running() >= self.config.max_running:
            raise TooManyRuns("too many Studio runs in flight; retry later")
        run = StudioRun(
            run_id=uuid.uuid4().hex,
            owner=auth.subject,
            spec=spec,
            spec_digest=spec.digest(),
            input=text,
        )
        self._runs[run.run_id] = run
        audit_event(
            "studio_run_started",
            subject=auth.subject,
            run_id=run.run_id,
            spec_name=spec.name,
            spec_digest=run.spec_digest,
        )
        run.task = asyncio.create_task(self._execute(run, auth), name=f"studio-{run.run_id}")
        return run

    def get(self, run_id: str, owner: str) -> StudioRun | None:
        """The run, if *owner* started it. Anyone else's is as unknown as a missing one."""
        run = self._runs.get(run_id)
        return run if run is not None and run.owner == owner else None

    def cancel(self, run: StudioRun) -> None:
        if run.task is not None and not run.task.done():
            run.task.cancel()

    async def shutdown(self) -> None:
        tasks = [r.task for r in self._runs.values() if r.task is not None and not r.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _on_exchange(self, run: StudioRun, node_id: str, exchange: RemoteExchange) -> None:
        run.emit(
            A2aMessage(
                node_id=node_id,
                remote=exchange.remote,
                skill=exchange.skill,
                direction=exchange.direction,
                method=exchange.method,
                task_id=exchange.task_id,
                context_id=exchange.context_id,
                task_state=exchange.task_state,
                states=list(exchange.states),
                parts=[PartSummary(kind=p.kind, size=p.size) for p in exchange.parts],
                elapsed_ms=exchange.elapsed_ms,
                status=exchange.status,
                card_digest=exchange.card_digest,
                content=capture(exchange.text) if self.config.capture_content else None,
            )
        )

    async def _execute(self, run: StudioRun, auth: AuthContext) -> None:
        spec = run.spec
        budgets = spec.budgets.resolved(self.config.ceilings)
        ledger = CompositionLedger(budgets)
        started = time.monotonic()
        run.emit(
            RunStart(
                spec_name=spec.name,
                spec_digest=run.spec_digest,
                pattern=spec.pattern,
                nodes=pipeline_order(spec),
                budgets=budgets.model_dump(),
                content=capture(run.input) if self.config.capture_content else None,
            )
        )
        compiled: CompiledComposition | None = None
        plugin: StudioRunPlugin | None = None
        outcome: RunOutcome = "failed"
        node_outcome: NodeOutcome = "failed"
        error: str | None = None
        try:
            compiled = compile_spec(
                spec,
                registry=self.registry,
                cards=self.cards,
                catalogue=self.catalogue(),
                model=self._model_factory() if self._model_factory else None,
                on_exchange=lambda node_id, ex: self._on_exchange(run, node_id, ex),
            )
            plugin = StudioRunPlugin(
                run, ledger, compiled, capture_content=self.config.capture_content
            )
            plugins = list(self._plugin_factory())
            _insert_before_error_handler(plugins, plugin)
            gateway = AgentGateway(
                app_name=APP_NAME,
                root_agent=compiled.workflow,
                plugins=plugins,
                session_service=InMemorySessionService(),
                memory_service=None,
                events_compaction_config=None,
                verified_confirmation=True,
                turn_timeout=float(budgets.timeout_s),
            )
            session_id = f"{STUDIO_SESSION_PREFIX}{run.run_id}"
            await gateway.sessions.create_session(
                app_name=APP_NAME, user_id=auth.subject, session_id=session_id
            )

            run_plugin = plugin

            async def stop_when_spent(_event: Any) -> None:
                if ledger.exhausted is not None:
                    raise _BudgetStop
                if run_plugin.failure is not None:
                    raise _NodeFailed

            await gateway.run_in_session(
                user_id=auth.subject,
                session_id=session_id,
                text=run.input,
                state_delta={AUTH_STATE_KEY: auth.as_state()},
                on_event=stop_when_spent,
                identity=self._caller_identity(auth) if self._caller_identity else None,
            )
            if ledger.exhausted is not None:
                outcome, node_outcome = "budget_exhausted", "budget_exhausted"
            elif plugin.failure is not None:
                raise _NodeFailed
            else:
                outcome, node_outcome = "succeeded", "ok"
        except _NodeFailed:
            assert plugin is not None and plugin.failure is not None
            agent_name, error_type = plugin.failure
            info = compiled.nodes.get(agent_name) if compiled is not None else None
            error = (
                f"model call failed in node {info.node_id if info else agent_name}: {error_type}"
            )
            outcome, node_outcome = "failed", "failed"
        except _BudgetStop:
            outcome, node_outcome = "budget_exhausted", "budget_exhausted"
        except TurnTimedOut:
            hit = ledger.deadline(time.monotonic() - started)
            run.emit(BudgetHit(budget=hit.budget, limit=hit.limit, used=hit.used))
            outcome, node_outcome = "budget_exhausted", "budget_exhausted"
        except asyncio.CancelledError:
            outcome, node_outcome = "cancelled", "cancelled"
        except Exception as exc:  # noqa: BLE001 - a failed run is an outcome, not a crash
            logger.exception("Studio run %s failed", run.run_id)
            error = type(exc).__name__
            outcome, node_outcome = "failed", "failed"
        finally:
            if compiled is not None:
                # Shielded: a cancelled run still closes its MCP sessions.
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await asyncio.shield(compiled.close())
            output = plugin.last_output() if plugin is not None else None
            if plugin is not None:
                plugin.close_open_nodes(node_outcome)
            if outcome == "budget_exhausted" and ledger.exhausted is not None:
                error = f"budget {ledger.exhausted.budget} reached"
            totals = ledger.totals(int((time.monotonic() - started) * 1000))
            run.status = outcome
            run.error = error
            run.totals = totals
            run.ended_at = time.time()
            run.emit(
                RunEnd(
                    outcome=outcome,
                    error=error,
                    totals=totals,
                    content=capture(output)
                    if self.config.capture_content and outcome == "succeeded"
                    else None,
                )
            )
            audit_event(
                "studio_run_ended",
                subject=auth.subject,
                run_id=run.run_id,
                outcome=outcome,
                tokens=totals.tokens.total,
                tool_calls=totals.tool_calls,
            )

    async def stream(self, run: StudioRun, after: int = 0) -> AsyncIterator[dict[str, Any] | None]:
        """Events with ``seq > after``, then each new one; ``None`` is a keepalive tick."""
        sent = after
        while True:
            while sent < len(run.events):
                event = run.events[sent]
                sent += 1
                yield event
            if run.finished and sent >= len(run.events):
                return
            await run.wait(15.0)
            if sent >= len(run.events) and not run.finished:
                yield None
