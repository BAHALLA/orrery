"""Orrery Studio (AEP-031): compose agents, remote A2A agents and MCP servers, and run them.

P0a: a sequential pipeline of ``llm_agent`` nodes, run apart from the live
coordinator, over an admin-only API (``/studio/*``). Modules:

- :mod:`.config` — ``ORRERY_STUDIO_*`` settings (no heavy imports);
- :mod:`.registry` — the ``ORRERY_STUDIO_REGISTRY`` file (MCP and A2A entries);
- :mod:`.spec` — the composition spec and its validation;
- :mod:`.events` — trace events v2, the stream a console renders;
- :mod:`.ledger` — the composition ledger (whole-run budgets);
- :mod:`.compiler` — spec → ADK ``Workflow``;
- :mod:`.runner` — runs, their events, retention;
- :mod:`.api` — the HTTP routes.
"""
