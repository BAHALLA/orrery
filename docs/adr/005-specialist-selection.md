# ADR-005: Specialists declare what they contribute

**Status:** Accepted
**Date:** 2026-10-17, amended the same day after review
**Author:** Taoufiq

The first version was accepted on the day it was implemented, without review,
and claimed more than the code did: a routing eval that did not exist, sweeps
that "cannot drift apart", layers that "only narrow", and a coordinator that
"knows none by name" while its remediation loop imported Kubernetes tools. An
independent review found these; the code was fixed where it could be, and this
record now states the rest as limits.

How the mechanism works end to end, its trust model and failure modes:
[How specialists are chosen](../specialist-selection.md).

## Context

Specialists registered through entry points, but the coordinator still carried
a table of facts about the built-in ones: a routing line per agent name, the
tools each contributes to a health sweep, the batch workflow's five checkers
and their report keys, the remediation loop's Kubernetes and Kafka tools, and
the ops journal's name and tools. A third-party specialist routed on its
`description` alone and could never join a sweep; every new built-in meant an
edit to the coordinator. The tables also produced bugs, each one a fact about
a specialist that outlived the specialist being switched off:

- the sweep called tools of disabled specialists (#253);
- the journal was offered after it was switched off (#255);
- with Kubernetes disabled, a degraded verdict still sent the batch loop into
  `restart_deployment` against it (only the confirmation gate stopped it);
- the batch fallback read a hand-kept list of report keys, so a third-party
  checker's "critical" was never read.

And any package in the environment, a transitive dependency included, was
loaded, routed to and placed in the coordinator's prompt.

## Decision

**A specialist declares what it contributes, as typed data that carries the
tools implementing it. Everything the coordinator and the batch workflow say
about a specialist, or do through one, is built from the declarations of the
enabled specialists.**

- `SpecialistSpec(agent, routing_hint, health_check, remediation, journal)`,
  registered as an entry point.
- Contributions are validated when loaded: health-check and verify tools must
  be the specialist's own reads (no guard, not named as a write); remediation
  actions must be guarded.
- **Third-party specialists are opt-in** (named in `ORRERY_SPECIALISTS`) and
  meet the built-ins' confirmation invariants at load time. Duplicate keys and
  agent names stop startup.
- The admin catalog may switch specialists off and add declarative agents;
  startup fails closed when the stored catalog cannot be applied.

## Alternatives considered

- **Keep the coordinator's tables** (status quo). Every fact about a specialist
  lives away from it and outlives it; the bugs above are what that costs.
- **A central manifest file** listing specialists and their hints. One place to
  read, but it separates a specialist's description from its code, so it drifts
  the same way, and a third party has to edit it. Entry points are Python's
  standard plugin mechanism and ship with the package.
- **Remote agents with A2A Agent Cards.** A network boundary gives real
  isolation and language independence, at the cost of latency, deployment and
  identity plumbing, and the guard decorators would no longer travel with the
  tools. The `SpecialistSpec` is close to an Agent Card in content; an A2A
  adapter could produce one later.
- **MCP-only extension.** Already supported (`ORRERY_MCP_CONFIG`) for tools, but
  MCP has no place for a health rubric, a remediation playbook or a journal,
  and its tools arrive unclassified (they are gated per call at run time).
- **A `health()` function per specialist** instead of a declared tuple of
  tools. Deterministic and cheaper, but it duplicates the tools' logic outside
  the reviewed tools, and the rubric would still need a model to apply it. A
  structured per-system verdict (below) keeps the tuple and removes the text
  parsing.
- **Capabilities as string flags** (`frozenset({"journal"})`, the first
  version). A flag has nothing behind it: the journal step still had to import
  the ops journal's tools, and a typo was silently ignored. Typed capabilities
  carry their interface.

## Consequences

- A new specialist, built-in or third party, is one package with one entry
  point. It routes, joins both sweeps, offers its remediation and its journal
  with no edit to the coordinator (a third-party one once named).
- Entry points that name a bare agent still work, with no contributions.
- The routing line's quality sits with the specialist's authors.
- Breaking for specialist authors: keys must be lowercase identifiers, and a
  plugin must be named in `ORRERY_SPECIALISTS`.

## Known limits

- **No coordinator routing eval.** `make eval` scores specialists one by one.
  Routing is the step this decision hands to third-party text, so it is the
  first eval to add.
- **The two sweeps share declarations, not behaviour.** The chat sweep follows
  the admin catalog and runs every check in one agent under one call budget
  (about ten sweepable specialists is its ceiling); the batch sweep follows
  the env at process start and runs per-system checkers in parallel. Building
  both from the same per-system checker would close both gaps.
- **Verdicts are text.** Checkers end with a `STATUS:` line and the batch
  fallback scans it for problem words. A structured per-system record
  (`record_system_status(system, status, evidence)`) would let routing read
  facts, and make "no record" mean unknown by construction. Candidate for
  ADR-006.
- **"Switched off" removes the route, not the tools** (declarative agents may
  still use them, by design). The env removes both.
- **The application is assembled at import time** in `orrery_assistant/agent.py`,
  and the admin and tests read its module globals. A single
  `build_application(settings)` would let the batch workflow follow the
  catalog and allow two configurations per test process.
- **Load-time checks cannot see inside toolsets** (MCP, OpenAPI), which list
  tools at run time; the confirmation gate still applies per call.
- **Checks catch mistakes, not malice.** A named plugin's code runs with the
  process's permissions; supply-chain controls are the defence there.
