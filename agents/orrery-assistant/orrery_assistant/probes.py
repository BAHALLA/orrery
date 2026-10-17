"""Read-only reachability probes for the console's first-run self-test.

Each specialist gets its single cheapest read-only tool. The question these
answer is not "is the cluster healthy" — the agent does that — but "is this
integration wired at all", which is the failure a new user actually hits and the
one the platform is worst at reporting: today a missing kubeconfig surfaces as a
stack trace inside a tool result, several turns into a conversation.

The probes come from the specialists themselves (``SpecialistSpec.setup_check``),
like everything else the coordinator knows about one: a specialist the
deployment switched off is not probed, its module is not imported for it, and a
third-party specialist gets a row by declaring a check. They run on an
authenticated but otherwise ungated path (the self-test does not consult RBAC),
which is why the registry refuses a setup check whose tool is not one of the
specialist's own reads.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from orrery_core.registry import Specialist
from orrery_core.serving.onboarding import IntegrationProbe


def _runner(tool: Any) -> Any:
    # A raw async function, or an ADK FunctionTool around one.
    func = getattr(tool, "func", tool)

    async def run() -> dict:
        return await func()

    return run


def default_probes(specialists: Sequence[Specialist]) -> list[IntegrationProbe]:
    """One probe per enabled specialist that declares a setup check."""
    return [
        IntegrationProbe(
            name=s.key,
            label=s.setup_check.label,
            hint=s.setup_check.hint,
            run=_runner(s.setup_check.tool),
        )
        for s in specialists
        if s.setup_check is not None
    ]
