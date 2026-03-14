"""Error callback factories for graceful tool and model failure handling.

Provides on_tool_error_callback and on_model_error_callback factories
that log errors and return user-friendly responses instead of crashing.

ADK callback signatures:
    on_tool_error:  (tool, args, tool_context, error) -> Optional[dict]
    on_model_error: (callback_context, llm_request, error) -> Optional[LlmResponse]
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.context import Context
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.base_tool import BaseTool
from google.genai import types

logger = logging.getLogger("ai_agents.errors")


def graceful_tool_error() -> Callable:
    """Create an on_tool_error_callback that returns a structured error dict.

    Instead of crashing the agent, the error is logged and returned as a
    tool result so the LLM can reason about the failure and try alternatives.

    Usage:
        create_agent(
            ...,
            on_tool_error_callback=graceful_tool_error(),
        )
    """

    def callback(
        tool: BaseTool,
        args: dict[str, Any],
        tool_context: Context,
        error: Exception,
    ) -> dict:
        logger.error("Tool '%s' failed: %s: %s", tool.name, type(error).__name__, error)
        return {
            "status": "error",
            "error_type": type(error).__name__,
            "message": f"Tool '{tool.name}' failed: {error}",
        }

    return callback


def graceful_model_error() -> Callable:
    """Create an on_model_error_callback that returns a friendly message.

    Instead of crashing, returns an LlmResponse telling the user the model
    call failed and suggesting they retry.

    Usage:
        create_agent(
            ...,
            on_model_error_callback=graceful_model_error(),
        )
    """

    def callback(
        callback_context: CallbackContext,
        llm_request: LlmRequest,
        error: Exception,
    ) -> LlmResponse:
        logger.error("Model call failed: %s: %s", type(error).__name__, error)
        return LlmResponse(
            content=types.Content(
                role="model",
                parts=[
                    types.Part.from_text(
                        text=(
                            f"I encountered an error communicating with the AI model: {error}. "
                            "Please try again."
                        )
                    )
                ],
            )
        )

    return callback
