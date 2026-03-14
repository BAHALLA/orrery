import os
from pathlib import Path
from typing import Any, Callable, Sequence

from dotenv import load_dotenv
from google.adk.agents import Agent


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
    sub_agents: Sequence[Agent] | None = None,
    before_tool_callback: Callable | list[Callable] | None = None,
    after_tool_callback: Callable | list[Callable] | None = None,
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
    if output_key:
        kwargs["output_key"] = output_key

    return Agent(**kwargs)
