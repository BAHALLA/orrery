import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google.adk.agents import Agent, ParallelAgent, SequentialAgent
from google.adk.agents.base_agent import BaseAgent


def load_agent_env(agent_file: str) -> None:
    """Load the .env file located next to the given agent module file.

    Usage in an agent's agent.py:
        load_agent_env(__file__)
    """
    env_path = Path(agent_file).parent / ".env"
    load_dotenv(dotenv_path=env_path)


def create_agent(
    *,
    name: str,
    description: str,
    instruction: str,
    tools: Sequence[Callable[..., Any]],
    model: str | None = None,
    sub_agents: Sequence[BaseAgent] | None = None,
    before_tool_callback: Callable | list[Callable] | None = None,
    after_tool_callback: Callable | list[Callable] | None = None,
    on_tool_error_callback: Callable | list[Callable] | None = None,
    on_model_error_callback: Callable | list[Callable] | None = None,
    output_key: str | None = None,
) -> Agent:
    """Create an ADK Agent with sensible defaults.

    The model defaults to the GEMINI_MODEL_VERSION env var, falling back
    to 'gemini-2.0-flash'.

    Args:
        before_tool_callback: Called before each tool execution. Return a dict
            to skip the tool (e.g., for guardrails), or None to proceed.
            Use guardrails.require_confirmation() or guardrails.dry_run().
        after_tool_callback: Called after each tool execution. Return a dict
            to override the result, or None to keep it.
            Use audit.audit_logger() for structured logging.
        on_tool_error_callback: Called when a tool raises an exception.
            Return a dict to use as the tool result (graceful recovery),
            or None to let the error propagate.
            Use error_handlers.graceful_tool_error().
        on_model_error_callback: Called when the model call fails.
            Return an LlmResponse to use instead, or None to propagate.
            Use error_handlers.graceful_model_error().
        output_key: Session state key to store this agent's output.
            Useful for passing results between agents in a SequentialAgent.
    """
    resolved_model = model or os.getenv("GEMINI_MODEL_VERSION", "gemini-2.0-flash")

    kwargs: dict[str, Any] = {
        "name": name,
        "model": resolved_model,
        "description": description,
        "instruction": instruction,
        "tools": list(tools),
    }

    if sub_agents:
        kwargs["sub_agents"] = list(sub_agents)
    if before_tool_callback:
        kwargs["before_tool_callback"] = before_tool_callback
    if after_tool_callback:
        kwargs["after_tool_callback"] = after_tool_callback
    if on_tool_error_callback:
        kwargs["on_tool_error_callback"] = on_tool_error_callback
    if on_model_error_callback:
        kwargs["on_model_error_callback"] = on_model_error_callback
    if output_key:
        kwargs["output_key"] = output_key

    return Agent(**kwargs)


def create_sequential_agent(
    *,
    name: str,
    description: str = "",
    sub_agents: Sequence[BaseAgent],
) -> SequentialAgent:
    """Create a SequentialAgent that runs sub-agents one after another.

    Sub-agents can pass data via output_key which writes to session state,
    making it available to subsequent agents in the sequence.
    """
    return SequentialAgent(name=name, description=description, sub_agents=list(sub_agents))


def create_parallel_agent(
    *,
    name: str,
    description: str = "",
    sub_agents: Sequence[BaseAgent],
) -> ParallelAgent:
    """Create a ParallelAgent that runs sub-agents concurrently.

    Each sub-agent runs in an isolated branch context. Use output_key on
    sub-agents to write results to session state for downstream agents.
    """
    return ParallelAgent(name=name, description=description, sub_agents=list(sub_agents))
