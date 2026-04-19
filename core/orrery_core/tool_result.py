"""Structured tool result type for cross-agent composition.

Existing tools return flat dicts (``{"status": "success", "topics": [...], "count": 5}``).
``ToolResult`` is a Pydantic model with the same external shape but adds:

- A typed ``status`` field (``success`` | ``error`` | ``partial``).
- Optional ``error_type`` for programmatic error handling.
- Optional ``remediation_hints`` — short, actionable strings a downstream agent
  (e.g., the remediation LoopAgent) can use to decide a next step.

New tools may return ``ToolResult`` directly; calling ``.to_dict()`` produces
the flat shape existing callers and tests expect, so adoption can be gradual.

Usage::

    from orrery_core import ToolResult

    async def get_topic_metadata(topic: str) -> dict:
        try:
            meta = await _fetch(topic)
        except NotFound:
            return ToolResult.error(
                f"Topic '{topic}' not found",
                error_type="TopicNotFound",
                hints=["Call list_kafka_topics to see available topics"],
            ).to_dict()
        return ToolResult.ok(topic=topic, partitions=meta.partitions).to_dict()
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ResultStatus = Literal["success", "error", "partial"]


class ToolResult(BaseModel):
    """Structured response from an agent tool."""

    model_config = ConfigDict(extra="forbid")

    status: ResultStatus = "success"
    message: str | None = None
    error_type: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    remediation_hints: list[str] = Field(default_factory=list)

    @classmethod
    def ok(
        cls,
        message: str | None = None,
        *,
        hints: list[str] | None = None,
        **data: Any,
    ) -> ToolResult:
        """Build a success result. Extra kwargs are attached as ``data``."""
        return cls(
            status="success",
            message=message,
            data=data,
            remediation_hints=hints or [],
        )

    @classmethod
    def error(
        cls,
        message: str,
        *,
        error_type: str = "ToolError",
        hints: list[str] | None = None,
        **data: Any,
    ) -> ToolResult:
        """Build an error result with an optional machine-readable ``error_type``."""
        return cls(
            status="error",
            message=message,
            error_type=error_type,
            data=data,
            remediation_hints=hints or [],
        )

    @classmethod
    def partial(
        cls,
        message: str,
        *,
        hints: list[str] | None = None,
        **data: Any,
    ) -> ToolResult:
        """Build a partial-success result (some items succeeded, some failed)."""
        return cls(
            status="partial",
            message=message,
            data=data,
            remediation_hints=hints or [],
        )

    def to_dict(self) -> dict[str, Any]:
        """Flatten into a backward-compatible dict.

        ``data`` keys are promoted to the top level so existing consumers
        that read ``result["topics"]`` continue to work.
        """
        out: dict[str, Any] = {"status": self.status}
        if self.message is not None:
            out["message"] = self.message
        if self.error_type is not None:
            out["error_type"] = self.error_type
        if self.remediation_hints:
            out["remediation_hints"] = list(self.remediation_hints)
        # data fields last so an explicit user-provided key wins over nothing,
        # but reserved fields (status/message/error_type) are never clobbered
        for key, value in self.data.items():
            if key in out:
                continue
            out[key] = value
        return out

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ToolResult:
        """Parse a flat dict (as produced by ``to_dict``) back into a ``ToolResult``.

        Unknown keys are collected into ``data``. ``status`` defaults to
        ``"success"`` when absent so legacy handlers keep working.
        """
        known = {"status", "message", "error_type", "remediation_hints"}
        status = payload.get("status", "success")
        if status not in ("success", "error", "partial"):
            # Unknown statuses (e.g. "confirmation_required" from guardrails)
            # are preserved by wrapping as success with message — the caller
            # can inspect data["original_status"] if needed.
            data = {k: v for k, v in payload.items() if k not in known}
            data["original_status"] = status
            return cls(
                status="success",
                message=payload.get("message"),
                data=data,
            )
        data = {k: v for k, v in payload.items() if k not in known}
        return cls(
            status=status,
            message=payload.get("message"),
            error_type=payload.get("error_type"),
            data=data,
            remediation_hints=list(payload.get("remediation_hints", [])),
        )
