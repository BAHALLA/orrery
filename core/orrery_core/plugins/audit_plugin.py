"""AuditPlugin — structured audit logging for every tool invocation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from ..observability.audit import attempt_logger, audit_logger


class AuditPlugin(BasePlugin):
    """Structured audit logging for every tool invocation.

    Emits two events per call: the *attempt* (before execution — registered
    ahead of the gate plugins so a call that is later denied or crashes still
    leaves a record) and the *outcome* (after execution — a gate's deny dict
    flows through as the result, so its status is audited too).

    Args:
        log_path: Optional path to also write a local .jsonl file.
        responses: How much of each result the outcome records: ``summary``
            (default), ``full`` or ``none``; ``None`` reads
            ``ORRERY_AUDIT_RESPONSES``.
    """

    def __init__(self, log_path: str | Path | None = None, *, responses: str | None = None) -> None:
        super().__init__(name="audit")
        self._attempt_callback = attempt_logger(log_path)
        self._callback = audit_logger(log_path, responses=responses)

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> None:
        """Record the attempt; always returns ``None`` so the chain continues."""
        return self._attempt_callback(tool=tool, args=tool_args, tool_context=tool_context)

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict,
    ) -> dict | None:
        """Record the outcome (including gate denials surfaced as the result)."""
        return self._callback(
            tool=tool, args=tool_args, tool_context=tool_context, tool_response=result
        )
