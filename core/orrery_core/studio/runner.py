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

A run lives in memory on the replica running it (bounded by count and age),
and is written to the :mod:`.store` as it goes: its summary and events, every
fraction of a second. Any replica then serves a run's summary and events from
the store, live or replayed; only the replica running it can cancel it.
Cancelling a run cancels its task: model and HTTP calls in flight are
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
from google.adk.tools.set_model_response_tool import SetModelResponseTool
from google.genai import types

from ..a2a.remote import RemoteCards, RemoteExchange
from ..observability.audit import audit_event
from ..plugins import CALL_BUDGET_STATUS, ErrorHandlerPlugin, default_plugins
from ..plugins.delegation_guard_plugin import is_delegation
from ..plugins.output_cap_plugin import ToolOutputCapPlugin
from ..plugins.token_budget_plugin import TokenBudgetPlugin
from ..reliability.resilience import ToolOutcome, classify_tool_outcome
from ..security.auth import AUTH_STATE_KEY, AuthContext
from ..security.identity import CallerIdentity
from ..security.principal import STUDIO_SESSION_PREFIX
from ..serving.gateway import AgentGateway, TurnTimedOut
from .compiler import (
    LABELLED_PREFIX,
    CompiledComposition,
    McpPins,
    NodeInfo,
    RouteFailed,
    compile_spec,
)
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
    graph_order,
    parse_spec,
    validate_spec,
)
from .store import (
    CompositionRecord,
    InMemoryStudioStore,
    RunRecord,
    StudioStore,
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
    composition_id: str | None = None
    composition_version: int | None = None
    created_at: float = field(default_factory=time.time)
    status: RunStatus = "running"
    ended_at: float | None = None
    error: str | None = None
    totals: Totals | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    dropped: int = 0
    task: asyncio.Task[None] | None = None
    #: Content capture (``ORRERY_STUDIO_CAPTURE_CONTENT``), its redaction and
    #: its per-run byte budget.
    capture_content: bool = False
    redact_ips: bool = False
    max_content_bytes: int = 256 * 1024
    content_bytes: int = 0
    _changed: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def finished(self) -> bool:
        return self.status != "running"

    def capture(self, text: str | None) -> str | None:
        """*text* as event content: ``None`` unless capture is on; redacted and cut."""
        if not self.capture_content:
            return None
        return capture(text, redact_ips=self.redact_ips)

    def emit(self, event: _Event) -> None:
        """Append *event* (numbered and stamped here), unless past the per-run cap.

        Captured content counts against :attr:`max_content_bytes`; past it the
        event is kept without its ``content``.
        """
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
        content = getattr(event, "content", None)
        if content:
            size = len(content.encode("utf-8"))
            if self.content_bytes + size > self.max_content_bytes:
                setattr(event, "content", None)  # noqa: B010 - not every event has the field
            else:
                self.content_bytes += size
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
            "pattern": self.spec.pattern,
            "composition_id": self.composition_id,
            "composition_version": self.composition_version,
            "created_at": self.created_at,
            "ended_at": self.ended_at,
            "error": self.error,
            "totals": self.totals.model_dump(mode="json") if self.totals else None,
            "event_count": len(self.events),
            "dropped_events": self.dropped,
            # This replica runs it: it can be cancelled here.
            "cancellable": not self.finished,
        }

    def record(self, replica: str, timeout_s: int) -> RunRecord:
        """The run as stored, for the other replicas."""
        return RunRecord(
            run_id=self.run_id,
            owner=self.owner,
            replica=replica,
            spec_name=self.spec.name,
            spec_digest=self.spec_digest,
            pattern=self.spec.pattern,
            status=self.status,
            created_at=self.created_at,
            deadline=self.created_at + timeout_s,
            composition_id=self.composition_id,
            composition_version=self.composition_version,
            ended_at=self.ended_at,
            error=self.error,
            totals=self.totals.model_dump(mode="json") if self.totals else None,
            event_count=len(self.events),
            dropped_events=self.dropped,
        )


def _entry_key(agent_name: str, context: Any) -> tuple[str, str]:
    """One entry of a node: its agent and the invocation running it."""
    return agent_name, str(getattr(context, "invocation_id", "") or "")


class StudioRunPlugin(BasePlugin):
    """Trace events and the composition ledger for one run. Fresh per run."""

    def __init__(
        self,
        run: StudioRun,
        ledger: CompositionLedger,
        compiled: CompiledComposition | None = None,
    ) -> None:
        super().__init__(name="studio_run")
        self._run = run
        self._ledger = ledger
        self._compiled = compiled or CompiledComposition(
            workflow=None,  # ty: ignore[invalid-argument-type]
            nodes={},
            mcp_tools={},
            order=[],
        )
        #: The answer of the graph node that finished last: the run's output.
        self._output: str | None = None
        # Keyed by (agent name, invocation id): each call of a supervisor's
        # child is its own invocation, so two at once are told apart, and a
        # node entered again (a loop) counts from zero.
        self._entered: dict[tuple[str, str], float] = {}
        self._tokens: dict[tuple[str, str], TokenCount] = {}
        self._last_text: dict[tuple[str, str], str] = {}
        #: Graph node id -> its latest answer (a loop that stops at its cap
        #: ends with the answer its source was judging).
        self._answers: dict[str, str] = {}
        self._tool_started: dict[str, float] = {}
        #: ``(agent name, error type)`` of the first failed model call. The
        #: error handler answers it with an apology, which would otherwise pass
        #: for the node's output and let the run "succeed".
        self.failure: tuple[str, str] | None = None
        #: ``(node id, function-call id)`` -> the inbound ``a2a.message`` held
        #: until that call's ``after_tool``, so its content is the reply *after*
        #: the safety screen and PII redaction (capture on only). Keyed by the
        #: call, not the node: a node's parallel calls finish in any order.
        self.pending_inbound: dict[tuple[str, str | None], A2aMessage] = {}

    def attach(self, compiled: CompiledComposition) -> None:
        """The compiled composition (built after the plugin, which it reports to)."""
        self._compiled = compiled

    # ── the graph's function nodes (compiler.Trace) ──
    def emit_event(self, event: Any) -> None:
        self._run.emit(event)

    def record_output(self, node_id: str, text: str) -> None:
        self._output = text
        self._answers[node_id] = text

    def answer_of(self, node_id: str) -> str | None:
        return self._answers.get(node_id)

    def hold_inbound(self, event: A2aMessage, call_id: str | None) -> None:
        key = (event.node_id, call_id)
        if (earlier := self.pending_inbound.pop(key, None)) is not None:
            self._run.emit(earlier)  # no call id to pair it by: sent without content
        self.pending_inbound[key] = event

    def _flush_inbound(
        self, node_id: str, call_id: str | None = None, reply: str | None = None
    ) -> None:
        """Emit the held event of one call with *reply* as its content."""
        if (event := self.pending_inbound.pop((node_id, call_id), None)) is not None:
            event.content = self._run.capture(reply)
            self._run.emit(event)

    def _flush_node(self, node_id: str) -> None:
        """Emit, without content, what a node's calls left held (a call that never returned)."""
        for key in [k for k in self.pending_inbound if k[0] == node_id]:
            self._run.emit(self.pending_inbound.pop(key))

    # ── nodes ──
    async def before_agent_callback(self, *, agent: Any, callback_context: Any) -> None:
        info = self._compiled.nodes.get(agent.name)
        if info is None:
            return None
        key = _entry_key(agent.name, callback_context)
        self._entered[key] = time.monotonic()
        self._tokens[key] = TokenCount()
        self._last_text.pop(key, None)
        self._run.emit(
            NodeEnter(node_id=info.node_id, kind=info.kind, model=info.model, parent=info.parent)
        )
        return None

    async def after_agent_callback(self, *, agent: Any, callback_context: Any) -> None:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            # ADK runs this hook while a cancel (or the deadline) unwinds the
            # node: leave it open, and the run's ``finally`` closes it with the
            # real outcome instead of "ok".
            return None
        failed = self.failure is not None and self.failure[0] == agent.name
        info = self._compiled.nodes.get(agent.name)
        key = _entry_key(agent.name, callback_context)
        # A labelled node's answer is set again by its route node, without the label.
        if (
            not failed
            and info is not None
            and info.parent is None
            and (text := self._last_text.get(key)) is not None
        ):
            self._output = text
            self._answers[info.node_id] = text
        self._exit(key, "failed" if failed else "ok")
        return None

    def _exit(self, key: tuple[str, str], outcome: NodeOutcome) -> None:
        info = self._compiled.nodes.get(key[0])
        started = self._entered.pop(key, None)
        if info is None or started is None:
            return
        self._flush_node(info.node_id)  # an exchange whose tool never finished
        self._run.emit(
            NodeExit(
                node_id=info.node_id,
                kind=info.kind,
                model=info.model,
                tokens=self._tokens.pop(key, TokenCount()),
                elapsed_ms=int((time.monotonic() - started) * 1000),
                outcome=outcome,
                parent=info.parent,
                content=self._run.capture(self._last_text.pop(key, None)),
            )
        )

    def labelled_nodes(self) -> list[NodeInfo]:
        return [info for info in self._compiled.nodes.values() if info.labels]

    def close_open_nodes(self, outcome: NodeOutcome) -> None:
        for key in list(self._entered):
            self._exit(key, outcome)

    def last_output(self) -> str | None:
        return self._output

    # ── models ──
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
            node = self._tokens.setdefault(_entry_key(name, callback_context), TokenCount())
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
                self._last_text[_entry_key(name, callback_context)] = text
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
        if isinstance(tool, SetModelResponseTool):
            return None  # ADK's structured-output plumbing, not a call the model chose
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
        if self._mcp_info(tool, tool_context) is not None:
            self._tool_started[_call_key(tool, tool_context)] = time.monotonic()
        return None

    async def after_tool_callback(
        self, *, tool: Any, tool_args: dict[str, Any], tool_context: Any, result: Any
    ) -> None:
        if getattr(tool, "delegates_remotely", False) is True:
            node = self._compiled.nodes.get(str(getattr(tool_context, "agent_name", "") or ""))
            if node is not None:
                reply = result.get("reply") if isinstance(result, dict) else None
                self._flush_inbound(
                    node.node_id,
                    _function_call_id(tool_context),
                    reply if isinstance(reply, str) else None,
                )
            return None
        info = self._mcp_info(tool, tool_context)
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

    def _mcp_info(self, tool: Any, tool_context: Any) -> Any:
        agent = str(getattr(tool_context, "agent_name", "") or "")
        return self._compiled.mcp_tools.get((agent, tool.name))

    def emit_budget(self, hit: Exhausted) -> None:
        self._run.emit(BudgetHit(budget=hit.budget, limit=hit.limit, used=hit.used))


class StudioSpentGate(BasePlugin):
    """Answers model calls once the run's ledger is spent, without calling the model.

    Its own plugin, placed before ``TokenBudgetPlugin``: that plugin reserves
    tokens in ``before_model``, and a short-circuit after it would leave the
    reservation unsettled (no ``after_model`` runs for a canned answer).
    """

    def __init__(self, ledger: CompositionLedger) -> None:
        super().__init__(name="studio_spent_gate")
        self._ledger = ledger

    async def before_model_callback(self, *, callback_context: Any, llm_request: Any) -> Any:
        if self._ledger.exhausted is None:
            return None
        # Spent: answer without buying more tokens; the run ends on the next event.
        return LlmResponse(
            content=types.Content(
                role="model", parts=[types.Part.from_text(text="The run's budget is spent.")]
            )
        )


def _function_call_id(tool_context: Any) -> str | None:
    raw = getattr(tool_context, "function_call_id", None)
    return raw if isinstance(raw, str) and raw else None


def _call_key(tool: Any, tool_context: Any) -> str:
    call_id = getattr(tool_context, "function_call_id", None)
    return str(call_id) if call_id else f"{tool.name}:{id(tool_context)}"


def _insert_before(plugins: list[BasePlugin], plugin: BasePlugin, *kinds: type) -> None:
    """Insert *plugin* before the first of *kinds* present, else at the end."""
    for kind in kinds:
        for i, existing in enumerate(plugins):
            if isinstance(existing, kind):
                plugins.insert(i, plugin)
                return
    plugins.append(plugin)


def _place_run_plugins(plugins: list[BasePlugin], run_plugin: BasePlugin, gate: BasePlugin) -> None:
    """Where the Studio's two plugins go in a run's stack.

    - the run plugin before ``ToolOutputCapPlugin``: ADK's after-tool chain
      stops at the first replacement, and the cap returns one, so after it the
      ``mcp.call`` event of a capped result would never be emitted (and
      ``ErrorHandlerPlugin`` must stay last anyway);
    - the spent gate before ``TokenBudgetPlugin`` (see :class:`StudioSpentGate`).
    """
    _insert_before(plugins, run_plugin, ToolOutputCapPlugin, ErrorHandlerPlugin)
    _insert_before(plugins, gate, TokenBudgetPlugin, ErrorHandlerPlugin)


def studio_plugins() -> list[BasePlugin]:
    """A fresh plugin list for one run: chat's stack, at L2, without memory."""
    return default_plugins(enable_auth=True, autonomy_level="L2", enable_memory=False)


class RunElsewhere(Exception):
    """A run that is still going on another replica: only that one can cancel it."""


#: Seconds between store writes of a run in flight (events are batched).
PERSIST_INTERVAL = 0.25
#: Seconds a run waits for its MCP sessions to close.
CLOSE_TIMEOUT = 10.0
#: Longest wait between attempts after a failed write.
PERSIST_MAX_BACKOFF = 5.0
#: Attempts at a finished run's last write before giving up.
PERSIST_FINAL_ATTEMPTS = 8
#: Seconds between store reads when following a run on another replica;
#: while nothing new comes, the wait grows to :data:`FOLLOW_MAX_INTERVAL`.
FOLLOW_INTERVAL = 1.0
FOLLOW_MAX_INTERVAL = 5.0
#: Runs on other replicas one person may follow at once (each polls the store).
MAX_FOLLOWS_PER_USER = 4
#: Seconds between retention purges.
PURGE_INTERVAL = 3600.0


class StudioService:
    """Validates specs, keeps compositions, starts runs and serves them."""

    def __init__(
        self,
        *,
        config: StudioConfig,
        registry: StudioRegistry,
        cards: RemoteCards,
        catalogue: Callable[[], Mapping[str, Any]] | None = None,
        model_factory: Callable[[], Any] | None = None,
        model_for: Callable[[str], Any] | None = None,
        plugin_factory: Callable[[], list[BasePlugin]] = studio_plugins,
        caller_identity: Callable[[AuthContext], CallerIdentity | None] | None = None,
        store: StudioStore | None = None,
    ) -> None:
        self.config = config
        self.registry = registry
        self.cards = cards
        self._catalogue = catalogue or dict
        self._model_factory = model_factory
        self._model_for = model_for
        self._plugin_factory = plugin_factory
        self._caller_identity = caller_identity
        self._runs: OrderedDict[str, StudioRun] = OrderedDict()
        #: MCP tool fingerprints first seen, for the process (not per run).
        self.mcp_pins = McpPins()
        self.store: StudioStore = store or InMemoryStudioStore(max_runs=config.max_runs)
        #: This process, in the store (a run is cancellable only where it runs).
        self.replica = uuid.uuid4().hex
        self._persisters: dict[str, asyncio.Task[None]] = {}
        #: Registry MCP entry -> why its tools could not be listed (``?resolve=true``).
        self.mcp_errors: dict[str, str] = {}
        self._purger: asyncio.Task[None] | None = None
        #: Subject -> streams following a run on another replica.
        self._follows: dict[str, int] = {}

    def catalogue(self) -> Mapping[str, Any]:
        return self._catalogue()

    def validation_context(self) -> ValidationContext:
        return ValidationContext(
            registry=self.registry,
            catalogue=self.catalogue(),
            ceilings=self.config.ceilings,
            models=self.config.models,
        )

    def check(self, raw: Mapping[str, Any]) -> CompositionSpec:
        """*raw* parsed and validated. Raises :class:`SpecInvalid` with every problem."""
        spec = parse_spec(raw)
        if errors := validate_spec(spec, self.validation_context()):
            raise SpecInvalid(errors)
        return spec

    # ── background work ──
    async def start_background(self) -> None:
        """Purge old runs now and every hour (the app's lifespan calls this)."""
        if self._purger is None:
            self._purger = asyncio.create_task(self._purge_loop(), name="studio-purge")

    async def _purge_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.purge)
            except Exception:  # noqa: BLE001 - a failed purge is retried on the next round
                logger.warning("Studio retention purge failed", exc_info=True)
            await asyncio.sleep(PURGE_INTERVAL)

    def purge(self, now: float | None = None) -> int:
        """Delete stored runs older than ``ORRERY_STUDIO_RUN_RETENTION_DAYS``."""
        now = time.time() if now is None else now
        purged = self.store.purge(older_than=now - self.config.run_retention_days * 86400)
        if purged:
            logger.info("Purged %d Studio runs past retention", purged)
        return purged

    # ── compositions ──
    def compositions(self) -> list[CompositionRecord]:
        return self.store.list_compositions()

    def composition(self, cid: str, version: int | None = None) -> CompositionRecord | None:
        return self.store.get_composition(cid, version)

    # ── runs ──
    def _prune(self, owner: str | None = None) -> None:
        """Forget expired runs, then the oldest finished ones past the caps.

        *owner* is about to start one: their oldest finished runs go first once
        they hold ``max_runs_per_user``, so one person cannot crowd out the rest.
        Forgotten here, a run is still in the store.
        """
        now = time.time()
        for run_id, run in list(self._runs.items()):
            if run.finished and now - (run.ended_at or now) > self.config.retention_seconds:
                del self._runs[run_id]
        if owner is not None:
            mine = [r for r in self._runs.values() if r.owner == owner]
            for run in [r for r in mine if r.finished][
                : max(0, len(mine) + 1 - self.config.max_runs_per_user)
            ]:
                del self._runs[run.run_id]
        while len(self._runs) > self.config.max_runs:
            oldest = next((r for r in self._runs.values() if r.finished), None)
            if oldest is None:
                break
            del self._runs[oldest.run_id]

    def running(self, owner: str | None = None) -> int:
        return sum(
            1 for r in self._runs.values() if not r.finished and (owner is None or r.owner == owner)
        )

    def start(
        self,
        spec: CompositionSpec,
        text: str,
        auth: AuthContext,
        *,
        composition: CompositionRecord | None = None,
    ) -> StudioRun:
        if not text.strip():
            raise SpecInvalid([SpecError(("input",), "input must not be empty")])
        self._prune(auth.subject)
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
            composition_id=composition.id if composition else None,
            composition_version=composition.version if composition else None,
            capture_content=self.config.capture_content,
            redact_ips=self.config.redact_ips,
            max_content_bytes=self.config.max_content_bytes_per_run,
        )
        self._runs[run.run_id] = run
        audit_event(
            "studio_run_started",
            subject=auth.subject,
            run_id=run.run_id,
            spec_name=spec.name,
            spec_digest=run.spec_digest,
            composition_id=run.composition_id,
            composition_version=run.composition_version,
        )
        run.task = asyncio.create_task(self._execute(run, auth), name=f"studio-{run.run_id}")
        run.task.add_done_callback(lambda task: self._finalise(run, task, auth))
        writer = asyncio.create_task(self._persist(run), name=f"studio-persist-{run.run_id}")
        self._persisters[run.run_id] = writer
        writer.add_done_callback(lambda _t: self._persisters.pop(run.run_id, None))
        return run

    def _finalise(self, run: StudioRun, task: asyncio.Task[None], auth: AuthContext) -> None:
        """End a run whose task stopped without ending it.

        A task cancelled before its first step never enters :meth:`_execute`,
        so its ``finally`` never runs: the run would stay ``running`` forever,
        holding one of its owner's slots. (Also covers a crash past the
        ``finally``, which should not happen.)
        """
        if run.finished:
            return
        outcome: RunOutcome = "cancelled" if task.cancelled() else "failed"
        run.status = outcome
        run.error = None if outcome == "cancelled" else "the run stopped unexpectedly"
        run.totals = Totals()
        run.ended_at = time.time()
        run.emit(RunEnd(outcome=outcome, error=run.error, totals=run.totals))
        audit_event("studio_run_ended", subject=auth.subject, run_id=run.run_id, outcome=outcome)

    async def _persist(self, run: StudioRun) -> None:
        """Write the run and its new events to the store until it ends (batched).

        Each write is one transaction (events and row), and a failed one is
        sent again, whole, with backoff: the append skips what is stored, so a
        commit in doubt is harmless. The last write is tried
        :data:`PERSIST_FINAL_ATTEMPTS` times before giving up.
        """
        timeout_s = run.spec.budgets.resolved(self.config.ceilings).timeout_s
        sent = 0
        first = True
        failures = 0
        while True:
            finished = run.finished
            batch = run.events[sent:]
            if batch or first or finished:
                record = run.record(self.replica, timeout_s)
                try:
                    await asyncio.to_thread(self.store.write_run, record, batch)
                except Exception:  # noqa: BLE001 - the run goes on; this replica still serves it
                    failures += 1
                    if failures == 1:
                        logger.warning("Writing Studio run %s failed", run.run_id, exc_info=True)
                    if finished and failures >= PERSIST_FINAL_ATTEMPTS:
                        logger.error(
                            "Gave up writing Studio run %s after %d attempts; other replicas "
                            "will report it lost",
                            run.run_id,
                            failures,
                        )
                        return
                    await asyncio.sleep(min(PERSIST_INTERVAL * 2**failures, PERSIST_MAX_BACKOFF))
                    continue
                if failures:
                    logger.info("Writing Studio run %s recovered", run.run_id)
                failures = 0
                sent += len(batch)
                first = False
            if finished and sent >= len(run.events):
                return
            # Events that came in during the write are not waited for: no
            # await between this check and wait() taking the current signal.
            if sent >= len(run.events) and not run.finished:
                await run.wait(15.0)
            await asyncio.sleep(PERSIST_INTERVAL)

    def get(self, run_id: str, owner: str) -> StudioRun | None:
        """The run, if it is on this replica and *owner* started it."""
        run = self._runs.get(run_id)
        return run if run is not None and run.owner == owner else None

    async def lookup(self, run_id: str, owner: str) -> StudioRun | RunRecord | None:
        """The run here, else as stored (any replica's); anyone else's is unknown."""
        if (run := self.get(run_id, owner)) is not None:
            return run
        stored = await asyncio.to_thread(self.store.get_run, run_id)
        if stored is None or stored.owner != owner:
            return None
        return stored.view()

    async def history(
        self, owner: str, *, composition_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """*owner*'s runs, newest first: this replica's and the stored ones.

        Only the store query leaves the event loop: the in-memory runs change
        on the loop, so they are read there.
        """
        records = await asyncio.to_thread(
            self.store.list_runs, owner, composition_id=composition_id, limit=limit
        )
        stored = {r.run_id: r.view().summary() | {"cancellable": False} for r in records}
        for run in self._runs.values():
            if run.owner == owner and (
                composition_id is None or run.composition_id == composition_id
            ):
                stored[run.run_id] = run.summary()
        return sorted(stored.values(), key=lambda r: -r["created_at"])[:limit]

    def cancel(self, run: StudioRun | RunRecord) -> None:
        """Cancel a run here; a stored run still going elsewhere raises :class:`RunElsewhere`."""
        if isinstance(run, RunRecord):
            if not run.finished:
                raise RunElsewhere(
                    "this run is on another replica; only that replica can cancel it "
                    "(route the console's requests to one replica, sticky sessions)"
                )
            return
        if run.task is not None and not run.task.done():
            run.task.cancel()

    async def shutdown(self) -> None:
        tasks = [r.task for r in self._runs.values() if r.task is not None and not r.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Let each run's last events reach the store.
        writers = [t for t in self._persisters.values() if not t.done()]
        if writers:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(5.0):
                    await asyncio.gather(*writers, return_exceptions=True)
        for task in writers:
            task.cancel()
        if self._purger is not None:
            self._purger.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._purger
            self._purger = None

    def _on_exchange(
        self,
        run: StudioRun,
        node_id: str,
        exchange: RemoteExchange,
        plugin: StudioRunPlugin | None = None,
    ) -> None:
        inbound = exchange.direction == "inbound"
        event = A2aMessage(
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
            # The request is what the node's model wrote; the reply is
            # captured after the plugins have screened it (below).
            content=None if inbound else run.capture(exchange.text),
        )
        if inbound and run.capture_content and plugin is not None:
            plugin.hold_inbound(event, exchange.call_id)
        else:
            run.emit(event)

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
                nodes=graph_order(spec),
                budgets=budgets.model_dump(),
                composition_id=run.composition_id,
                composition_version=run.composition_version,
                content=run.capture(run.input),
            )
        )
        compiled: CompiledComposition | None = None
        plugin = StudioRunPlugin(run, ledger)
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
                model_for=self._model_for,
                on_exchange=lambda node_id, ex: self._on_exchange(run, node_id, ex, plugin),
                mcp_pins=self.mcp_pins,
                trace=plugin,
            )
            plugin.attach(compiled)
            plugins = list(self._plugin_factory())
            _place_run_plugins(plugins, plugin, StudioSpentGate(ledger))
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

            async def stop_when_spent(_event: Any) -> None:
                if ledger.exhausted is not None:
                    raise _BudgetStop
                if plugin.failure is not None:
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
            assert plugin.failure is not None
            agent_name, error_type = plugin.failure
            info = compiled.nodes.get(agent_name) if compiled is not None else None
            error = (
                f"model call failed in node {info.node_id if info else agent_name}: {error_type}"
            )
            outcome, node_outcome = "failed", "failed"
        except RouteFailed as exc:
            error = str(exc)
            outcome, node_outcome = "failed", "failed"
        except _BudgetStop:
            outcome, node_outcome = "budget_exhausted", "budget_exhausted"
        except TurnTimedOut:
            hit = ledger.deadline(time.monotonic() - started)
            run.emit(BudgetHit(budget=hit.budget, limit=hit.limit, used=hit.used))
            outcome, node_outcome = "budget_exhausted", "budget_exhausted"
        except asyncio.CancelledError:
            # Swallowed on purpose (the run ends as an outcome); say so, so an
            # enclosing timeout or TaskGroup does not read it as still pending.
            if (task := asyncio.current_task()) is not None:
                task.uncancel()
            outcome, node_outcome = "cancelled", "cancelled"
        except Exception as exc:  # noqa: BLE001 - a failed run is an outcome, not a crash
            route_failed = _route_failure(exc)
            unlabelled = _unlabelled_node(exc, plugin)
            if route_failed is not None:
                error = str(route_failed)
            elif unlabelled is not None:
                node_id, labels = unlabelled
                error = (
                    f"node {node_id!r} answered without one of its labels {labels}: its "
                    "answer was not the structured {label, answer} it must give"
                )
            else:
                logger.exception("Studio run %s failed", run.run_id)
                error = type(exc).__name__
            outcome, node_outcome = "failed", "failed"
        finally:
            if compiled is not None:
                # Shielded: a cancelled run still closes its MCP sessions.
                # Bounded: a server that never answers its close does not
                # keep the run (and its person's slot) open.
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(compiled.close()), CLOSE_TIMEOUT)
            output = plugin.last_output()
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
                    content=run.capture(output) if outcome == "succeeded" else None,
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

    async def stream(
        self, run: StudioRun | RunRecord, after: int = 0
    ) -> AsyncIterator[dict[str, Any] | None]:
        """Events with ``seq > after``, then each new one; ``None`` is a keepalive tick."""
        if isinstance(run, RunRecord):
            async for item in self._follow(run, after):
                yield item
            return
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

    def following(self, subject: str) -> int:
        return self._follows.get(subject, 0)

    def start_follow(self, subject: str) -> None:
        self._follows[subject] = self._follows.get(subject, 0) + 1

    def end_follow(self, subject: str) -> None:
        left = self._follows.get(subject, 0) - 1
        if left > 0:
            self._follows[subject] = left
        else:
            self._follows.pop(subject, None)

    async def _follow(self, run: RunRecord, after: int) -> AsyncIterator[dict[str, Any] | None]:
        """A stored run's events: a replay, or a run going on another replica, followed."""
        sent = after
        idle = 0.0
        interval = FOLLOW_INTERVAL
        while True:
            # The row first: once it reads finished, every event is stored.
            current = await asyncio.to_thread(self.store.get_run, run.run_id)
            events = await asyncio.to_thread(self.store.events, run.run_id, after=sent)
            for event in events:
                sent = int(event.get("seq", sent))
                yield event
            if current is None or current.view().finished:
                return
            # Back off while nothing happens; back to fast once events come.
            interval = FOLLOW_INTERVAL if events else min(interval * 1.5, FOLLOW_MAX_INTERVAL)
            await asyncio.sleep(interval)
            idle = 0.0 if events else idle + interval
            if idle >= 15.0:
                idle = 0.0
                yield None


def _chain(exc: BaseException) -> list[BaseException]:
    out: list[BaseException] = []
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        if any(current is seen for seen in out):
            continue
        out.append(current)
        stack.extend(x for x in (current.__cause__, current.__context__) if x is not None)
        if isinstance(current, BaseExceptionGroup):
            stack.extend(current.exceptions)
    return out


def _unlabelled_node(exc: BaseException, plugin: StudioRunPlugin) -> tuple[str, list[str]] | None:
    """``(node id, labels)`` when *exc* is a labelled node's answer failing its schema.

    Only the ``Labelled_<id>`` model counts: any other validation error (a bad
    tool argument, say) is not a missing label.
    """
    from pydantic import ValidationError

    for error in _chain(exc):
        if isinstance(error, ValidationError) and error.title.startswith(LABELLED_PREFIX):
            node_id = error.title[len(LABELLED_PREFIX) :]
            for info in plugin.labelled_nodes():
                if info.node_id == node_id:
                    return node_id, list(info.labels)
    return None


def _route_failure(exc: BaseException) -> RouteFailed | None:
    """The :class:`RouteFailed` behind *exc*, if ADK wrapped one."""
    return next((e for e in _chain(exc) if isinstance(e, RouteFailed)), None)
