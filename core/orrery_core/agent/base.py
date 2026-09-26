from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.agents.base_agent import BaseAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.planners import BasePlanner
from google.adk.tools.base_tool import BaseTool

from ..observability.log import setup_logging
from ..security.guardrails import ACTOR_STATE_KEY
from .config import DEFAULT_GEMINI_MODEL

logger = logging.getLogger("orrery.base")

# Appended to the system instruction each turn when the caller is known, so the
# model always knows *who* is asking. Critical in a shared channel thread where
# the caller changes per message and the model would otherwise conflate the
# human with a tool's service account.
_IDENTITY_BLOCK = """

## Who you are talking to (this turn)
The person making THIS request is **__CALLER__**. A shared thread's caller can
change with every message — so act for THIS person on THIS turn. "me", "my",
"mine", "I" mean **__CALLER__**, never you. Your integrations authenticate as
*service accounts* — those are NOT the caller and must never be reported as the
user's own identity. For "my …" / "assigned to me" queries, resolve THIS
caller's own account and filter by it; never use a tool's service identity as
if it were the human."""


def _caller_identity(ctx: Any) -> str | None:
    """Best-effort caller for the current run, from session state.

    Prefers the gateway-stamped ``actor``; falls back to the verified JWT
    subject the HTTP front door stores under ``_auth``.
    """
    state = getattr(ctx, "state", None)
    get = getattr(state, "get", None)
    if not callable(get):
        return None
    actor = get(ACTOR_STATE_KEY)
    if actor:
        return str(actor)
    auth = get("_auth")
    subject = auth.get("subject") if isinstance(auth, dict) else None
    return str(subject) if subject else None


class _IdentityAwareInstruction:
    """An ADK ``InstructionProvider`` appending the caller's identity per turn."""

    def __init__(self, instruction: str) -> None:
        self.base_instruction = instruction

    def __call__(self, ctx: Any) -> str:
        caller = _caller_identity(ctx)
        if not caller:
            return self.base_instruction
        return self.base_instruction + _IDENTITY_BLOCK.replace("__CALLER__", caller)


def identity_aware_instruction(instruction: str) -> _IdentityAwareInstruction:
    """Wrap a prompt so it appends the current caller's identity each turn.

    Returns an ADK ``InstructionProvider`` (a callable). Passing a callable
    also makes ADK use the text **verbatim** — no ``{var}`` state templating —
    so prompts may safely contain literal braces (JSON examples, label
    placeholders). When no caller is bound (e.g. ``adk web`` with no transport
    identity), the base instruction is returned unchanged.

    The original text stays reachable via :func:`base_instruction` (the
    provider carries a ``base_instruction`` attribute).
    """
    return _IdentityAwareInstruction(instruction)


def base_instruction(agent_or_instruction: Any) -> str:
    """Return the raw instruction text of an agent or instruction value.

    Unwraps the ``identity_aware_instruction`` provider; a plain string is
    returned as-is. Useful in tests that assert on prompt content.
    """
    value = getattr(agent_or_instruction, "instruction", agent_or_instruction)
    return getattr(value, "base_instruction", value)


def load_agent_env(agent_file: str | None = None) -> None:
    """Load the .env file for the agent.

    By default, it searches for a .env file in the current working directory
    and its parents (centralized configuration). If ``agent_file`` is provided
    and a .env file exists in the same directory, it is loaded with precedence.

    Also configures structured JSON logging to stdout on first call.

    Usage in an agent's agent.py:
        load_agent_env(__file__)
    """
    # 1. Load centralized .env (searches CWD and parents)
    load_dotenv()

    # 2. Fallback/Override: local .env next to the agent module
    if agent_file:
        local_env = Path(agent_file).parent / ".env"
        if local_env.exists():
            load_dotenv(dotenv_path=local_env, override=True)

    setup_logging()


def resolve_model() -> str | BaseLlm:
    """Resolve the LLM model from environment variables.

    Reads MODEL_PROVIDER and MODEL_NAME to determine which backend to use.
    For Gemini (the default), returns a plain model string.
    For other providers (anthropic, openai, ollama, etc.), returns a LiteLlm instance.

    Environment variables:
        MODEL_PROVIDER: "gemini" (default), "anthropic", "openai", "ollama", etc.
        MODEL_NAME: Model identifier (e.g., "gemini-2.5-pro", "anthropic/claude-sonnet-4-20250514").
        GEMINI_MODEL_VERSION: Legacy alias for MODEL_NAME when provider is gemini.

    Returns:
        A model string for Gemini or a LiteLlm instance for other providers.
    """
    provider = os.getenv("MODEL_PROVIDER", "gemini").lower()

    if provider == "gemini":
        return os.getenv("MODEL_NAME") or os.getenv("GEMINI_MODEL_VERSION") or DEFAULT_GEMINI_MODEL

    # Non-Gemini provider — use LiteLlm
    from google.adk.models.lite_llm import LiteLlm

    model_name = os.getenv("MODEL_NAME", "")
    if not model_name:
        raise ValueError(
            f"MODEL_NAME must be set when MODEL_PROVIDER={provider}. "
            f"Example: MODEL_NAME=anthropic/claude-sonnet-4-20250514"
        )

    model_name = _prefix_provider(model_name, provider)
    logger.info("Using LiteLlm with model: %s", model_name)
    return LiteLlm(model=model_name)


def _prefix_provider(model_name: str, provider: str) -> str:
    """Qualify *model_name* with *provider* unless it is already qualified.

    LiteLlm routes on a ``provider/name`` string (e.g. ``anthropic/claude-...``).
    """
    return model_name if "/" in model_name else f"{provider}/{model_name}"


#: Summarizer default for event compaction. Compaction rewrites conversation
#: history into a digest — plumbing, not user-facing reasoning — so it runs on a
#: cheap model regardless of what the agent itself is configured with.
DEFAULT_COMPACTION_MODEL = "gemini-flash-latest"


def resolve_summarizer_model() -> BaseLlm:
    """Resolve the model used to summarize compacted conversation history.

    Unlike :func:`resolve_model`, this always returns a ``BaseLlm`` *instance* —
    ADK's ``LlmEventSummarizer(llm=...)`` takes an object, not a model string.

    Environment variables:
        ORRERY_COMPACTION_MODEL: Summarizer model. Defaults to
            ``gemini-flash-latest`` on Gemini; on other providers it defaults to
            that provider's ``MODEL_NAME`` (there is no cross-provider cheap
            default we can assume exists).
        MODEL_PROVIDER: Selects the backend, as in :func:`resolve_model`.
    """
    provider = os.getenv("MODEL_PROVIDER", "gemini").lower()
    override = os.getenv("ORRERY_COMPACTION_MODEL", "").strip()

    if provider == "gemini":
        from google.adk.models.google_llm import Gemini

        return Gemini(model=override or DEFAULT_COMPACTION_MODEL)

    from google.adk.models.lite_llm import LiteLlm

    model_name = override or os.getenv("MODEL_NAME", "")
    if not model_name:
        raise ValueError(
            f"ORRERY_COMPACTION_MODEL or MODEL_NAME must be set when "
            f"MODEL_PROVIDER={provider}. Example: ORRERY_COMPACTION_MODEL=anthropic/claude-haiku-4-5"
        )
    return LiteLlm(model=_prefix_provider(model_name, provider))


_SAFETY_THRESHOLDS = frozenset(
    {"BLOCK_NONE", "BLOCK_ONLY_HIGH", "BLOCK_MEDIUM_AND_ABOVE", "BLOCK_LOW_AND_ABOVE"}
)


def resolve_safety_config() -> Any | None:
    """Build a ``GenerateContentConfig`` with Gemini content-safety filters.

    Applied by ``create_agent()`` to Gemini models only (LiteLLM-routed
    providers do not consume the genai safety settings). On by default;
    ``GEMINI_SAFETY_FILTERS=false`` disables. The block threshold comes from
    ``GEMINI_SAFETY_THRESHOLD`` (default ``BLOCK_ONLY_HIGH`` — SRE work talks
    about killing pods and destroying volumes all day, so the aggressive
    thresholds trip on legitimate operations chatter).

    Returns ``None`` when disabled or when the provider is not Gemini.
    """
    enabled = os.getenv("GEMINI_SAFETY_FILTERS", "").strip().lower()
    if enabled in ("0", "false", "no", "off"):
        return None
    if os.getenv("MODEL_PROVIDER", "gemini").lower() != "gemini":
        return None

    from google.genai import types

    threshold_name = os.getenv("GEMINI_SAFETY_THRESHOLD", "BLOCK_ONLY_HIGH").strip().upper()
    if threshold_name not in _SAFETY_THRESHOLDS:
        logger.warning("Unknown GEMINI_SAFETY_THRESHOLD %r; using BLOCK_ONLY_HIGH.", threshold_name)
        threshold_name = "BLOCK_ONLY_HIGH"
    threshold = types.HarmBlockThreshold(threshold_name)

    return types.GenerateContentConfig(
        safety_settings=[
            types.SafetySetting(category=category, threshold=threshold)
            for category in (
                types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
            )
        ]
    )


def resolve_planner() -> BasePlanner | None:
    """Resolve an ADK planner from environment variables.

    Read ``ORRERY_PLANNER`` to select the planner attached to opted-in agents
    (typically the root orchestrator). Planning adds an explicit reasoning
    step before tool calls — useful for orchestration agents that route
    across multiple specialists, less useful for narrow tool-leaf agents.

    Values:
        ``none`` (default): no planner.
        ``plan_react``: ``PlanReActPlanner`` — provider-agnostic; structures
            the model output into PLANNING / ACTION / REASONING / FINAL_ANSWER
            phases. Works with all providers supported by ``resolve_model``.
        ``builtin``: ``BuiltInPlanner`` — uses Gemini's native thinking
            tokens. Falls back to ``None`` with a warning when
            ``MODEL_PROVIDER`` is not ``gemini``, since LiteLLM-routed models
            do not consume the ADK thinking config.

    Additional env vars (only consulted when ``ORRERY_PLANNER=builtin``):
        ``ORRERY_PLANNER_THINKING_BUDGET``: int token budget for thinking.
        ``ORRERY_PLANNER_INCLUDE_THOUGHTS``: bool (default ``true``) — include
            the model's thoughts in the response stream.

    Returns:
        A ``BasePlanner`` instance or ``None`` to skip planning.
    """
    choice = os.getenv("ORRERY_PLANNER", "none").lower().strip()

    if choice in ("", "none"):
        return None

    if choice == "plan_react":
        from google.adk.planners import PlanReActPlanner

        logger.info("Using PlanReActPlanner")
        return PlanReActPlanner()

    if choice == "builtin":
        provider = os.getenv("MODEL_PROVIDER", "gemini").lower()
        if provider != "gemini":
            logger.warning(
                "ORRERY_PLANNER=builtin requires MODEL_PROVIDER=gemini "
                "(current: %s); falling back to no planner.",
                provider,
            )
            return None

        from google.adk.planners import BuiltInPlanner
        from google.genai import types

        thinking_kwargs: dict[str, Any] = {}
        if budget := os.getenv("ORRERY_PLANNER_THINKING_BUDGET"):
            thinking_kwargs["thinking_budget"] = int(budget)
        include = os.getenv("ORRERY_PLANNER_INCLUDE_THOUGHTS", "true").lower()
        thinking_kwargs["include_thoughts"] = include in ("1", "true", "yes")

        logger.info("Using BuiltInPlanner with thinking_config=%s", thinking_kwargs)
        return BuiltInPlanner(thinking_config=types.ThinkingConfig(**thinking_kwargs))

    logger.warning("Unknown ORRERY_PLANNER value %r; using no planner.", choice)
    return None


def create_agent(
    *,
    name: str,
    description: str,
    instruction: str,
    tools: Sequence[Callable[..., Any] | BaseTool],
    model: str | BaseLlm | None = None,
    planner: BasePlanner | None = None,
    sub_agents: Sequence[BaseAgent] | None = None,
    before_tool_callback: Callable | list[Callable] | None = None,
    after_tool_callback: Callable | list[Callable] | None = None,
    on_tool_error_callback: Callable | list[Callable] | None = None,
    on_model_error_callback: Callable | list[Callable] | None = None,
    output_key: str | None = None,
    mode: Literal["chat", "task", "single_turn"] | None = None,
) -> Agent:
    """Create an ADK Agent with sensible defaults.

    The model is resolved from environment variables via resolve_model()
    unless explicitly passed. Supports Gemini (default), Claude, OpenAI,
    and any LiteLLM-compatible provider.

    Args:
        model: Explicit model override. Can be a Gemini model string or a
            BaseLlm instance (e.g., LiteLlm). When None, resolved from env.
        planner: Optional ADK planner attached to this agent (e.g.,
            ``PlanReActPlanner`` or ``BuiltInPlanner``). Use
            ``resolve_planner()`` to read the choice from env. Reserve this
            for orchestration agents — tool-leaf agents do not benefit.
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
            Useful for passing results between nodes in a graph Workflow
            (downstream nodes read it from session state).
        mode: ADK 2.0 delegation mode — 'chat' (conversational, keeps history),
            'task', or 'single_turn'. When None, ADK infers it: 'chat' as a
            sub-agent, 'single_turn' as a graph node. Set 'chat' explicitly on
            a Workflow/App root coordinator so it retains conversation history.
    """
    resolved_model = model if model is not None else resolve_model()

    kwargs: dict[str, Any] = {
        "name": name,
        "model": resolved_model,
        # Gemini content-safety filters (AEP-013). Only attached when the
        # resolved model is a Gemini string — LiteLLM models ignore the
        # genai config, so attaching it there would be misleading.
        **(
            {"generate_content_config": safety_config}
            if isinstance(resolved_model, str) and (safety_config := resolve_safety_config())
            else {}
        ),
        "description": description,
        # Passed as an InstructionProvider so ADK uses the prompt verbatim (no
        # {var} state templating — literal braces are safe) AND so the current
        # caller's identity is appended each turn when a transport stamped one.
        # See identity_aware_instruction.
        "instruction": identity_aware_instruction(instruction),
        "tools": list(tools),
    }

    if mode is not None:
        kwargs["mode"] = mode
    if planner is not None:
        kwargs["planner"] = planner
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
