"""Fold a turn's ProgressSteps into "who was asked, and what did they run".

The web console does this in TypeScript (``web/src/chat/trace.ts``); chat
transports that can only edit a message — Slack, Google Chat — need the same
view server-side. :class:`ProgressTracker` is the transport-neutral half: it
consumes steps from :func:`~orrery_core.plugins.progress_scope` and answers
questions; rendering (mrkdwn, cards) stays with each transport.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Literal

from ..plugins.progress_plugin import ProgressStep

CallState = Literal["running", "ok", "error", "refused"]

_AGENT_LABELS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p), label)
    for p, label in (
        (r"^kafka", "Kafka"),
        (r"^k8s", "Kubernetes"),
        (r"^elasticsearch|^es_", "Elasticsearch"),
        (r"^observability", "Observability"),
        (r"^docker", "Docker"),
        (r"^ops_journal|^journal", "Ops journal"),
        (r"^incident_triage", "Incident triage"),
    )
)


def agent_label(name: str) -> str:
    """``kafka_health_agent`` → ``Kafka``; unknown names become readable words."""
    for pattern, label in _AGENT_LABELS:
        if pattern.match(name):
            return label
    words = re.sub(r"_(mcp_agent|agent|specialist)$", "", name).replace("_", " ")
    return words[:1].upper() + words[1:]


@dataclass
class TrackedCall:
    call_id: str
    agent: str
    tool: str
    delegation: bool
    guarded: bool
    state: CallState = "running"
    elapsed_ms: int | None = None
    children: list[TrackedCall] = field(default_factory=list)


@dataclass
class ProgressTracker:
    """Calls seen so far in one turn, nested under the delegation that made them.

    Pass :meth:`apply` to ``progress_scope`` as the sink. Not thread-safe; the
    sink runs on the event loop.
    """

    calls: list[TrackedCall] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)
    _by_id: dict[str, TrackedCall] = field(default_factory=dict)

    def apply(self, step: ProgressStep) -> None:
        call = self._by_id.get(step.call_id)
        if step.phase == "start" or call is None:
            call = TrackedCall(
                call_id=step.call_id,
                agent=step.agent,
                tool=step.tool,
                delegation=step.delegation,
                guarded=step.guarded,
            )
            self._by_id[step.call_id] = call
            parent = self._running_delegation(self.calls, step.agent)
            (parent.children if parent else self.calls).append(call)
        if step.phase == "end":
            call.state = step.outcome or "ok"
            call.elapsed_ms = step.elapsed_ms

    def _running_delegation(self, calls: list[TrackedCall], agent: str) -> TrackedCall | None:
        for call in reversed(calls):
            if not call.delegation:
                continue
            if nested := self._running_delegation(call.children, agent):
                return nested
            if call.tool == agent and call.state == "running":
                return call
        return None

    def all_calls(self) -> list[TrackedCall]:
        out: list[TrackedCall] = []
        stack = list(reversed(self.calls))
        while stack:
            call = stack.pop()
            out.append(call)
            stack.extend(reversed(call.children))
        return out

    @property
    def has_calls(self) -> bool:
        return bool(self.calls)

    def summary(self) -> str:
        """``Kafka, Kubernetes · 4 tool calls · 7.2s`` — the finished-turn receipt."""
        calls = self.all_calls()
        consulted = list(dict.fromkeys(agent_label(c.tool) for c in calls if c.delegation))
        tools = [c for c in calls if not c.delegation]
        parts = [", ".join(consulted)] if consulted else []
        parts.append(f"{len(tools)} tool call{'' if len(tools) == 1 else 's'}")
        if refused := sum(c.state == "refused" for c in calls):
            parts.append(f"{refused} awaiting approval or refused")
        parts.append(f"{time.monotonic() - self.started_at:.1f}s")
        return " · ".join(parts)
