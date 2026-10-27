"""Human approvals in Studio runs (AEP-031 §3, P0c).

A run pauses where a person must decide: at a ``human_approval`` node. The
run's task waits here, on a future only :meth:`RunApprovals.decide` resolves,
and that is reached only through ``POST /studio/runs/{id}/approvals/{aid}``,
called by the human admin who started the run. Nothing a model writes, and
nothing a client sends to the run itself, can answer it.

This is not ADK's ``RequestInput`` resume: a run is one task held in memory on
the replica running it, so waiting in that task is enough, and no resume event
exists for a client to forge. The cost is that, like cancel, a decision must
reach the replica running the run (any other answers 409).

- **What the person sees** is the text under review, redacted (credentials,
  emails), held in memory only and served to the run's owner
  (``GET /studio/runs/{id}/approvals``). Events carry its digest and size, and
  its content only with ``ORRERY_STUDIO_CAPTURE_CONTENT``.
- **Approve names the digest** of the text it saw, so an approval can never
  apply to text the person did not see. Reject needs no digest.
- **Once**: a second decision is refused (409).
- **Default deny**: an approval not decided within
  ``ORRERY_STUDIO_APPROVAL_TIMEOUT_SECONDS`` expires, and the run ends
  ``rejected``. A cancelled run cancels its pending approvals.
- **The run's own deadline is paused** while any approval waits: a person's
  time is not the run's compute budget (:attr:`RunApprovals.on_waiting`).
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from ..security.redaction import redact_text
from .events import MAX_APPROVALS_PER_RUN, ApprovalDecided, ApprovalRequested

ApprovalKind = Literal["approval"]
Decision = Literal["approved", "rejected", "expired", "cancelled"]

#: Text under review kept for the person, in UTF-8 bytes (the rest is cut).
MAX_REVIEW_BYTES = 64 * 1024
#: A reviewer's comment, in characters.
MAX_COMMENT_CHARS = 2000
#: Decided approval ids remembered per run, to answer a second decision with 409.
MAX_DECIDED = 256


def digest_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def review_text(text: str, *, redact_ips: bool = False) -> str:
    """*text* as a person reviews it: redacted, and cut to :data:`MAX_REVIEW_BYTES`."""
    redacted, _ = redact_text(text, redact_emails=True, redact_ips=redact_ips)
    data = redacted.encode("utf-8")
    if len(data) <= MAX_REVIEW_BYTES:
        return redacted
    return data[: MAX_REVIEW_BYTES - 3].decode("utf-8", "ignore") + "…"


@dataclass(frozen=True)
class ApprovalOutcome:
    decision: Decision
    comment: str | None = None

    @property
    def approved(self) -> bool:
        return self.decision == "approved"


@dataclass
class PendingApproval:
    approval_id: str
    node_id: str
    kind: ApprovalKind
    prompt: str
    #: What the person reviews (redacted, cut); the digest is of the original.
    text: str
    digest: str
    size: int
    requested_at: float
    expires_at: float
    future: asyncio.Future[ApprovalOutcome] = field(repr=False)

    def view(self, *, with_text: bool) -> dict[str, Any]:
        data: dict[str, Any] = {
            "approval_id": self.approval_id,
            "node_id": self.node_id,
            "kind": self.kind,
            "prompt": self.prompt,
            "digest": self.digest,
            "size": self.size,
            "requested_at": self.requested_at,
            "expires_at": self.expires_at,
        }
        if with_text:
            data["text"] = self.text
        return data


class ApprovalNotFound(LookupError):
    """No pending approval with that id in this run."""


class TooManyApprovals(Exception):
    """A run asked for more than :data:`MAX_APPROVALS_PER_RUN` approvals (a loop around one)."""


class ApprovalConflict(Exception):
    """Already decided, or approved against text the person did not see."""


class RunApprovals:
    """The pending approvals of one run, and the only way to decide them."""

    def __init__(
        self,
        *,
        timeout_s: float,
        emit: Callable[[Any], None],
        capture: Callable[[str | None], str | None],
        redact_ips: bool = False,
    ) -> None:
        self._timeout_s = timeout_s
        self._emit = emit
        self._capture = capture
        self._redact_ips = redact_ips
        self._pending: dict[str, PendingApproval] = {}
        self._decided: dict[str, Decision] = {}
        self._asked = 0
        #: Called with ``True`` when the first approval starts waiting and with
        #: ``False`` when the last one stops (the runner pauses its deadline).
        self.on_waiting: Callable[[bool], None] | None = None

    @property
    def waiting(self) -> bool:
        return bool(self._pending)

    def pending(self) -> list[PendingApproval]:
        return list(self._pending.values())

    def latest_expiry(self) -> float | None:
        return max((p.expires_at for p in self._pending.values()), default=None)

    async def request(
        self, *, node_id: str, prompt: str, text: str, kind: ApprovalKind = "approval"
    ) -> ApprovalOutcome:
        """Wait for the person's decision on *text*; ``expired`` past the timeout.

        A cancel of the waiting task cancels the approval (``approval.decided``
        with ``cancelled``) and propagates. Raises :class:`TooManyApprovals`
        past :data:`MAX_APPROVALS_PER_RUN`.
        """
        self._asked += 1
        if self._asked > MAX_APPROVALS_PER_RUN:
            raise TooManyApprovals(f"the run asked for more than {MAX_APPROVALS_PER_RUN} approvals")
        now = time.time()
        loop = asyncio.get_running_loop()
        pending = PendingApproval(
            approval_id=uuid.uuid4().hex,
            node_id=node_id,
            kind=kind,
            prompt=prompt,
            text=review_text(text, redact_ips=self._redact_ips),
            digest=digest_of(text),
            size=len(text.encode("utf-8")),
            requested_at=now,
            expires_at=now + self._timeout_s,
            future=loop.create_future(),
        )
        self._pending[pending.approval_id] = pending
        self._emit(
            ApprovalRequested(
                node_id=node_id,
                approval_id=pending.approval_id,
                kind=kind,
                prompt=prompt,
                digest=pending.digest,
                size=pending.size,
                expires_at=pending.expires_at,
                content=self._capture(text),
            )
        )
        if len(self._pending) == 1 and self.on_waiting is not None:
            self.on_waiting(True)
        outcome = ApprovalOutcome("cancelled")
        try:
            try:
                outcome = await asyncio.wait_for(
                    asyncio.shield(pending.future), timeout=self._timeout_s
                )
            except TimeoutError:
                outcome = ApprovalOutcome("expired")
        finally:
            # A decision racing the timeout (or a cancel) loses: the outcome
            # stands as it was settled here.
            if not pending.future.done():
                pending.future.cancel()
            self._pending.pop(pending.approval_id, None)
            self._remember(pending.approval_id, outcome.decision)
            self._emit(
                ApprovalDecided(
                    node_id=node_id,
                    approval_id=pending.approval_id,
                    kind=kind,
                    decision=outcome.decision,
                    waited_ms=int((time.time() - now) * 1000),
                    content=self._capture(outcome.comment),
                )
            )
            if not self._pending and self.on_waiting is not None:
                self.on_waiting(False)
        return outcome

    def decide(
        self,
        approval_id: str,
        *,
        approve: bool,
        digest: str | None = None,
        comment: str | None = None,
    ) -> ApprovalOutcome:
        """Resolve a pending approval. Raises :class:`ApprovalNotFound` or :class:`ApprovalConflict`."""
        pending = self._pending.get(approval_id)
        if pending is None:
            if approval_id in self._decided:
                raise ApprovalConflict(f"this approval was already {self._decided[approval_id]}")
            raise ApprovalNotFound(approval_id)
        if pending.future.done():
            raise ApprovalConflict("this approval was already decided")
        if approve and digest != pending.digest:
            raise ApprovalConflict(
                "the digest does not match the text under review; read it again before approving"
            )
        outcome = ApprovalOutcome(
            "approved" if approve else "rejected",
            (comment or "").strip()[:MAX_COMMENT_CHARS] or None,
        )
        pending.future.set_result(outcome)
        return outcome

    def cancel_all(self) -> None:
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.cancel()

    def _remember(self, approval_id: str, decision: Decision) -> None:
        self._decided[approval_id] = decision
        while len(self._decided) > MAX_DECIDED:
            self._decided.pop(next(iter(self._decided)))
