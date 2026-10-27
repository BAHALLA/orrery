"""The composition ledger (AEP-031 §2): budgets for a whole Studio run.

AEP-027's budgets count per *agent run* (one agent in one invocation). A
composition is several agents, so the Studio adds one ledger for the run:
tokens (from each model response's ``usage_metadata``), tool calls,
delegations, remote calls, and the deadline (enforced by the gateway's turn
timeout). Whichever runs out first ends the run with a ``budget.hit`` event.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .events import BudgetName, TokenCount, Totals
from .spec import ResolvedBudgets


@dataclass(frozen=True)
class Exhausted:
    budget: BudgetName
    limit: int
    used: int


@dataclass
class CompositionLedger:
    budgets: ResolvedBudgets
    tokens: TokenCount = field(default_factory=TokenCount)
    tokens_by_model: dict[str, TokenCount] = field(default_factory=dict)
    tool_calls: int = 0
    delegations: int = 0
    remote_calls: int = 0
    #: The first budget that ran out; the run ends on it.
    exhausted: Exhausted | None = None

    def _check(self, budget: BudgetName, used: int, limit: int) -> Exhausted | None:
        if used > limit and self.exhausted is None:
            self.exhausted = Exhausted(budget, limit, used)
            return self.exhausted
        return None

    def charge_tokens(self, model: str, prompt: int, output: int, total: int) -> Exhausted | None:
        total = total or prompt + output
        for count in (self.tokens, self.tokens_by_model.setdefault(model, TokenCount())):
            count.input += prompt
            count.output += output
            count.total += total
        return self._check("max_tokens", self.tokens.total, self.budgets.max_tokens)

    def charge_tool(self, *, delegation: bool, remote: bool) -> Exhausted | None:
        """Count one tool call about to run; the exhausted budget if this one is past it."""
        self.tool_calls += 1
        if hit := self._check("max_tool_calls", self.tool_calls, self.budgets.max_tool_calls):
            return hit
        if delegation:
            self.delegations += 1
            if hit := self._check(
                "max_delegations", self.delegations, self.budgets.max_delegations
            ):
                return hit
        if remote:
            self.remote_calls += 1
            return self._check("max_remote_calls", self.remote_calls, self.budgets.max_remote_calls)
        return None

    def charge_remote(self) -> Exhausted | None:
        """Count one more message on a remote call already counted (a person's answer)."""
        self.remote_calls += 1
        return self._check("max_remote_calls", self.remote_calls, self.budgets.max_remote_calls)

    def deadline(self, elapsed_s: float) -> Exhausted:
        if self.exhausted is None:
            self.exhausted = Exhausted("timeout_s", self.budgets.timeout_s, int(elapsed_s))
        return self.exhausted

    def totals(self, wall_ms: int) -> Totals:
        return Totals(
            tokens=self.tokens.model_copy(),
            tokens_by_model={k: v.model_copy() for k, v in self.tokens_by_model.items()},
            tool_calls=self.tool_calls,
            delegations=self.delegations,
            remote_calls=self.remote_calls,
            wall_ms=wall_ms,
        )
