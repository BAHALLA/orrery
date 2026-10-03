"""Name-based mutation detection — a safety net for the guard decorators.

Orrery decides what a tool *does* from metadata, not from its name: RBAC, the
autonomy levels and the confirmation gate all read the ``@confirm`` /
``@destructive`` decorators. That is the right source of truth, and it has one
failure mode: a mutating tool someone forgot to decorate is silently treated as
a **read**. No confirmation card, no L2 block, a ``viewer`` able to call it,
and an audit trail recording a write as a lookup. Nothing fails; the gap is
invisible until the call that exploits it.

:func:`looks_mutating` is the cross-check. It flags a tool whose *name* says it
changes something, so a test can require that every such tool either carries a
decorator or appears on a reviewed list of tools that only write the agent's own
state. It never gates anything at runtime — the decorators stay the only
authority — it only makes forgetting one fail the build.

Matching is by ``_``-delimited **token**, not prefix, so it works for both
``delete_topic`` and ``kafka_delete_topic``. A name that *starts* with a read
verb is a read whatever follows (``get_loki_label_values`` is not a write
because "label" appears in it). An unknown verb is not flagged — this is a net
for the common verbs, not a classifier, and false alarms would teach people to
exempt without reading.
"""

from __future__ import annotations

#: Tokens that denote a state change when they appear in a tool name.
MUTATING_VERBS: frozenset[str] = frozenset(
    {
        "add",
        "alter",
        "apply",
        "approve",
        "assign",
        "cordon",
        "create",
        "decrease",
        "delete",
        "disable",
        "drain",
        "drop",
        "enable",
        "evict",
        "exec",
        "expire",
        "increase",
        "kill",
        "patch",
        "pause",
        "promote",
        "prune",
        "purge",
        "push",
        "put",
        "reassign",
        "remove",
        "rename",
        "reset",
        "restart",
        "resume",
        "rollback",
        "rollout",
        "rotate",
        "save",
        "scale",
        "send",
        "set",
        "silence",
        "start",
        "stop",
        "suspend",
        "taint",
        "trigger",
        "truncate",
        "tune",
        "uncordon",
        "unpause",
        "update",
        "upsert",
        "write",
    }
)

#: Leading tokens that make a name a read regardless of what follows.
READ_VERBS: frozenset[str] = frozenset(
    {
        "analyze",
        "check",
        "count",
        "describe",
        "diagnose",
        "explain",
        "fetch",
        "find",
        "get",
        "inspect",
        "list",
        "load",
        "lookup",
        "query",
        "read",
        "search",
        "show",
        "summarize",
        "top",
        "validate",
        "view",
        "watch",
    }
)


def mutating_tokens(tool_name: str) -> frozenset[str]:
    """The mutating verbs in *tool_name*, or an empty set if it reads as a read."""
    tokens = [t for t in tool_name.lower().split("_") if t]
    if not tokens or tokens[0] in READ_VERBS:
        return frozenset()
    return frozenset(tokens) & MUTATING_VERBS


def looks_mutating(tool_name: str) -> bool:
    """Whether *tool_name* says it changes something (see the module docstring)."""
    return bool(mutating_tokens(tool_name))
