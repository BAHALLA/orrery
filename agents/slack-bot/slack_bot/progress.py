"""Live progress in a Slack thread: one status message, edited as the turn runs.

Slack can't render a stream the way the web console does, but it can edit a
message. While the agent works, a single status message lists each specialist
consulted and its tool calls; when the turn ends it is replaced by a one-line
receipt (or deleted, if the turn ran no tools at all) and the answer is posted
as before.

Everything here is best-effort: a missing client, a rate limit, or a revoked
scope is logged and the turn carries on — progress must never cost an answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from orrery_core.serving.progress import ProgressTracker, TrackedCall, agent_label

logger = logging.getLogger("slack_bot.progress")

#: Minimum seconds between edits. chat.update is Tier 3 (~50/min); one edit
#: every 2 s stays well inside it even with several threads busy.
UPDATE_INTERVAL_SECONDS = 2.0

_ICON = {
    "running": ":hourglass_flowing_sand:",
    "ok": ":white_check_mark:",
    "error": ":warning:",
    "refused": ":lock:",
}


def _call_line(call: TrackedCall) -> str:
    timing = f" _{call.elapsed_ms / 1000:.1f}s_" if call.elapsed_ms is not None else ""
    return f"{_ICON[call.state]} `{call.tool}`{timing}"


def render_live(tracker: ProgressTracker) -> str:
    """The status message while the turn is running (Slack mrkdwn)."""
    lines = [":hourglass_flowing_sand: *Working…*"]
    for call in tracker.calls:
        if call.delegation:
            lines.append(f"{_ICON[call.state]} *{agent_label(call.tool)}*")
            lines.extend(f"      {_call_line(c)}" for c in call.children if not c.delegation)
            lines.extend(
                f"      {_ICON[c.state]} *{agent_label(c.tool)}*"
                for c in call.children
                if c.delegation
            )
        else:
            lines.append(_call_line(call))
    return "\n".join(lines)


def render_done(tracker: ProgressTracker) -> str:
    """The receipt left in the thread once the turn is over."""
    return f":white_check_mark: {tracker.summary()}"


class SlackProgress:
    """Owns one turn's status message. Use as an async context manager.

    ``sink`` goes to ``progress_scope``. The message is posted at the first
    refresh tick that has something to show — so a turn that finishes within
    :data:`UPDATE_INTERVAL_SECONDS`, or runs no tools, adds nothing to the
    thread but its answer. Progress is for the turns slow enough to need it.
    """

    def __init__(self, *, say: Any, channel: str, thread_ts: str) -> None:
        self._say = say
        self._client = getattr(say, "client", None)
        self._channel = channel
        self._thread_ts = thread_ts
        self.tracker = ProgressTracker()
        self._ts: str | None = None
        self._last_text: str | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def sink(self, step: Any) -> None:
        self.tracker.apply(step)

    async def __aenter__(self) -> SlackProgress:
        if self.enabled:
            self._task = asyncio.create_task(self._refresh_loop())
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(Exception):
                await self._task
        if self._ts is None:
            return
        if self.tracker.has_calls:
            await self._update(render_done(self.tracker))
        else:
            await self._call("chat_delete", channel=self._channel, ts=self._ts)

    async def _refresh_loop(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=UPDATE_INTERVAL_SECONDS)
            if self._stop.is_set() or not self.tracker.has_calls:
                continue
            text = render_live(self.tracker)
            if self._ts is None:
                await self._post(text)
            elif text != self._last_text:
                await self._update(text)

    async def _post(self, text: str) -> None:
        try:
            response = await self._say(text=text, thread_ts=self._thread_ts)
            self._ts = response["ts"] if response else None
            self._last_text = text
        except Exception:
            logger.warning("Could not post the progress message", exc_info=True)

    async def _update(self, text: str) -> None:
        if await self._call("chat_update", channel=self._channel, ts=self._ts, text=text):
            self._last_text = text

    async def _call(self, method: str, **kwargs: Any) -> bool:
        try:
            await getattr(self._client, method)(**kwargs)
            return True
        except Exception:
            logger.warning("Slack %s failed for the progress message", method, exc_info=True)
            return False
