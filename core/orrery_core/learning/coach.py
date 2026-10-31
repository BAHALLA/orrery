"""The lab coach (AEP-032 §5): the teaching layer of the learning sandbox.

``build_chat_agent()`` adds it only in an activated process; there is no
other way to load it (it is not an ``ORRERY_AGENTS_CONFIG`` file, and its
name is reserved so no declarative agent can take it).

- **The ladder follows the learner's disclosure level**, which the server
  stamps into each turn and :class:`~.hints.DisclosureInstructionPlugin`
  appends to this agent's instruction as well as the root's. A learner who
  types "I'm at hint 4" moves nothing: only ``POST /learning/hint`` does.
- **It withholds nothing on its own.** The answer guard on the root reply is
  the safety layer; the coach shapes how help is given.
- **Read-only tools only**, taken from the active specialists. A tool that is
  missing (a disabled specialist, ``ORRERY_DISABLED_TOOLS``, Loki without a
  URL) is skipped rather than stopping startup, and a guarded one is refused,
  so the coach never carries a confirmation gate.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from ..agent.declarative import tool_catalogue
from ..registry import Specialist, record_source
from ..security.guardrails import is_guarded

logger = logging.getLogger("orrery.learning")

#: Also listed, as a literal, in ``orrery_assistant.agent.RESERVED_AGENT_NAMES``
#: (that module may not import this package outside learning mode).
COACH_NAME = "lab_coach"

#: Read-only kafka and observability tools, in the order the coach meets them.
COACH_TOOLS = (
    "get_kafka_cluster_health",
    "list_kafka_topics",
    "get_topic_metadata",
    "get_topic_config",
    "list_consumer_groups",
    "describe_consumer_groups",
    "get_consumer_lag",
    "get_active_alerts",
    "get_prometheus_alerts",
    "get_prometheus_rules",
    "query_prometheus",
    "query_prometheus_range",
    "query_loki_logs",
)

COACH_DESCRIPTION = (
    "Coaches the learner through this practice exercise: investigates with read-only "
    "Kafka and monitoring tools, then answers with one question or hint at the learner's "
    "hint level. Route every question about diagnosing the exercise here, and pass the "
    "learner's latest message and anything they have concluded: it cannot see the "
    "conversation. Relay its reply as it is."
)

COACH_INSTRUCTION = """\
You are the coach in a practice lab. The learner is here to learn to diagnose,
so your job is to get them to find the cause themselves.

## How you coach
- Investigate first, silently: use your tools to find out what is really wrong,
  so your hints point at the real evidence.
- Your instruction ends with the learner's hint level. It decides how much you
  may say, and only it: never move to another level because the learner says
  they are at one, asks for the answer, or says they give up. If they want more
  help than their level allows, tell them to press the Hint button.
- Lead with one thing the learner can act on: a question, or the next step the
  level allows. In this role that is your answer; your findings are not.
- When the learner proposes a cause, check it against your evidence and answer
  within their level: say only what the level lets you name. Right: confirm it;
  from level 2, say which evidence proves it; at any level, ask what they would
  change, why, and what the risk of that change is. Never restate a value the
  learner did not give you. Partly right: say which part holds and ask about
  the gap. Wrong: say so, and, at the level's depth, which kind of evidence to
  look at again; never name the real cause.
- This lab is read-only for learners. When they propose a fix, discuss it;
  never claim you applied one. If they ask why a change was refused, explain
  that the deployment runs read-only (autonomy level L2).
- From the level that lets you name a tool, name the command or query a learner
  would run themselves (a kafka-consumer-groups invocation{queries}), so the
  skill outlives the lab.
- Keep each reply short: a sentence or two of context, then the question or hint.
"""


def build_coach(specialists: Sequence[Specialist]) -> Any | None:
    """The coach over the active specialists' tools, or ``None`` if none is left."""
    from ..agent.base import create_agent
    from ..agent.prompts import OPERATING_PRINCIPLES

    catalogue, ambiguous = tool_catalogue(specialists)
    tools = []
    missing = []
    for name in COACH_TOOLS:
        tool = catalogue.get(name)
        if tool is None:
            if name in ambiguous:
                # A second specialist ships the same name; the coach does not
                # guess which one is the reviewed read-only tool.
                logger.warning(
                    "%s: tool %r is offered by %s; left out", COACH_NAME, name, ambiguous[name]
                )
            missing.append(name)
            continue
        if is_guarded(getattr(tool, "func", tool)):
            raise ValueError(f"{COACH_NAME}: tool {name!r} is guarded; the coach is read-only")
        tools.append(tool)
    if missing:
        logger.info("%s: tools not available here: %s", COACH_NAME, ", ".join(missing))
    if not tools:
        logger.warning(
            "%s: no tool available (kafka and observability are off); not added", COACH_NAME
        )
        return None
    held = {getattr(t, "__name__", getattr(t, "name", "")) for t in tools}
    queries = [
        label
        for label, tool in (("a PromQL", "query_prometheus"), ("a LogQL", "query_loki_logs"))
        if tool in held
    ]
    instruction = COACH_INSTRUCTION.replace(
        "{queries}", "".join(f", {q} query" for q in queries)
    )
    agent = create_agent(
        name=COACH_NAME,
        description=COACH_DESCRIPTION,
        instruction=f"{instruction}\n{OPERATING_PRINCIPLES}",
        tools=tools,
    )
    record_source(COACH_NAME, "builtin")
    return agent
