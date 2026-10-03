"""Model-call resilience: a retry policy, and a chain of models to fall back on.

Orrery's resilience layer (``ResiliencePlugin``, ``@with_retry``) is scoped to
*tools*. Nothing protected the model call itself, so one ``503`` from the
provider failed the turn, and a provider incident was a full outage of every
replica at once. Two mechanisms close that, and they answer different failures:

* **Retry** (:func:`resolve_retry_options`) absorbs a *blip* on one model — a
  ``429`` burst, a ``503`` that clears in seconds. Attached to every agent's
  ``generate_content_config``: Gemini reads it natively, and ADK's ``LiteLlm``
  maps ``attempts`` onto LiteLLM's ``num_retries``.
* **Fallback** (:class:`FallbackLlm`) covers a model being *unavailable for
  longer than the retries last*. Retrying the same model harder does nothing
  when that model's capacity is what is gone; a second model does.

Retries run inside each link of the chain, so a dead primary costs one full
retry budget before the chain moves on — keep ``ORRERY_LLM_RETRY_ATTEMPTS``
modest when a chain is configured.

**What moves the chain on.** ``404`` (a retired preview model answers 404, and
no retry brings a pinned name back), ``408``, ``429`` and ``5xx``, plus
timeouts and dropped connections. **Never ``400``**: a malformed request fails
identically on every model, so falling back would multiply one user's error by
the length of the chain, turn a fast failure into a slow one, and bill for each
attempt. Auth failures (``401``/``403``) do not move it either — they are a
configuration error that a different model under the same credentials will
repeat.

**It will not switch models mid-answer.** Once a response has been yielded, the
turn is committed to that model, and a failure after that point is re-raised
untouched — splicing a second model's output onto a first model's partial
stream would hand the user a reply neither model wrote.

**One family per chain.** ADK formats tool-call history differently for Gemini
than for LiteLLM-routed models (the latter pair calls with results by id, and
ADK only preserves those ids when the *agent's* model is a LiteLLM model), so a
Gemini primary falling back to a LiteLLM model mid-conversation sends history
the fallback cannot pair. :func:`resolve_model_chain` therefore refuses a chain
that mixes the two, at startup, rather than letting the first failover fail in
a way nobody can read.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from google.adk.models.base_llm import BaseLlm
from pydantic import Field, PrivateAttr

if TYPE_CHECKING:
    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types

logger = logging.getLogger("orrery.fallback")

#: Failures worth trying another model for. See the module docstring for why
#: 404 is in and 400/401/403 are out.
FALLBACK_STATUSES = frozenset({404, 408, 429, 500, 502, 503, 504})

#: Statuses the per-model retry policy retries. 404 is deliberately absent: a
#: retired model does not come back, so retrying it only delays the fallback.
RETRY_STATUSES = [408, 429, 500, 502, 503, 504]

#: How far up the ``__cause__``/``__context__`` chain to look for the HTTP
#: status. Providers wrap their transport errors, so the code is often a level
#: or two below the exception that surfaces.
_CAUSE_DEPTH = 5

#: Seconds a model that just failed is skipped before it is tried first again.
#: Short: long enough that a turn in the middle of an outage does not pay the
#: dead model's whole retry budget on every model call, short enough that the
#: primary is back in use soon after it recovers.
DEFAULT_COOLDOWN_SECONDS = 30.0

_TRUTHY_OFF = {"0", "false", "no", "off"}


def status_of(exc: BaseException) -> int | None:
    """Best-effort HTTP status for *exc*, or ``None`` if it carries none.

    ``google.genai`` raises ``APIError`` with ``.code``; LiteLLM's exceptions
    carry ``.status_code``. Read by attribute rather than ``isinstance`` so this
    needs no import of a provider SDK that may not be installed.
    """
    seen: BaseException | None = exc
    for _ in range(_CAUSE_DEPTH):
        if seen is None:
            break
        for attr in ("code", "status_code"):
            value = getattr(seen, attr, None)
            if isinstance(value, int) and 100 <= value < 600:
                return value
        seen = seen.__cause__ or seen.__context__
    return None


def should_fall_back(exc: BaseException) -> bool:
    """Whether *exc* says the *model* failed, rather than the request."""
    status = status_of(exc)
    if status is not None:
        return status in FALLBACK_STATUSES
    return isinstance(exc, (TimeoutError, ConnectionError))


class LlmChainExhaustedError(RuntimeError):
    """Every model in the chain failed with a fallback-worthy error."""


class FallbackLlm(BaseLlm):
    """Answer on the first model in ``targets`` that can.

    Args:
        targets: Models to try, primary first. At least two.
        cooldown_seconds: After a model fails, how long it is tried *last*
            rather than first. A model is never skipped outright: if every
            model is cooling down, all are still tried in order.
    """

    targets: list[BaseLlm] = Field(min_length=2)
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    _cooling_until: dict[int, float] = PrivateAttr(default_factory=dict)

    def __init__(self, **data: Any) -> None:
        targets = data.get("targets") or []
        data.setdefault("model", targets[0].model if targets else "")
        super().__init__(**data)

    @property
    def capabilities(self) -> Any:
        """The primary's capabilities: it is the model the agent was built for."""
        return self.targets[0].capabilities

    def _order(self) -> list[int]:
        """Target indexes to try: healthy ones in order, then cooling ones."""
        now = time.monotonic()
        healthy = [i for i in range(len(self.targets)) if self._cooling_until.get(i, 0) <= now]
        cooling = [i for i in range(len(self.targets)) if i not in healthy]
        return healthy + cooling

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        # Snapshot before the first attempt: a provider may mutate the request
        # it was handed (Gemini appends a user turn and strips fields), and the
        # next model must see the request the agent built, not a leftover.
        pristine = llm_request.model_copy(deep=True)
        order = self._order()
        last_exc: BaseException | None = None

        for position, index in enumerate(order):
            target = self.targets[index]
            request = llm_request if position == 0 else pristine.model_copy(deep=True)
            request.model = target.model
            yielded = False
            try:
                async for response in target.generate_content_async(request, stream=stream):
                    yielded = True
                    yield response
            except Exception as exc:
                if yielded or not should_fall_back(exc):
                    # Committed to this model, or the request itself is at
                    # fault: either way another model cannot help.
                    raise
                last_exc = exc
                self._cooling_until[index] = time.monotonic() + self.cooldown_seconds
                nxt = order[position + 1] if position + 1 < len(order) else None
                logger.warning(
                    "Model %s failed (%s, status=%s); %s",
                    target.model,
                    type(exc).__name__,
                    status_of(exc),
                    f"falling back to {self.targets[nxt].model}"
                    if nxt is not None
                    else "no model left to fall back to",
                )
                if nxt is not None:
                    _track_failover(target.model, self.targets[nxt].model)
                continue
            else:
                self._cooling_until.pop(index, None)
                return

        raise LlmChainExhaustedError(
            f"all {len(self.targets)} models in the fallback chain failed"
        ) from last_exc


def _track_failover(from_model: str, to_model: str) -> None:
    """Count a failover; a missing metrics backend never fails the turn."""
    try:
        from ..observability.metrics import track_llm_failover

        track_llm_failover(from_model=from_model, to_model=to_model)
    except Exception:  # pragma: no cover - metrics are best-effort
        logger.debug("could not record LLM failover metric", exc_info=True)


def resolve_retry_options() -> types.HttpRetryOptions | None:
    """Build the per-model retry policy from the environment, or ``None``.

    Environment variables:
        ORRERY_LLM_RETRY_ATTEMPTS: *Total* tries per model call (default ``4``).
            ``0`` or ``1`` disables retrying — one attempt is just the call.
        ORRERY_LLM_RETRY_INITIAL_DELAY: First backoff, seconds (default ``1``).
        ORRERY_LLM_RETRY_MAX_DELAY: Backoff ceiling, seconds (default ``30``).
    """
    from google.genai import types

    raw = os.getenv("ORRERY_LLM_RETRY_ATTEMPTS", "").strip().lower()
    if raw in _TRUTHY_OFF and raw:
        return None
    try:
        attempts = int(raw) if raw else 4
        initial = float(os.getenv("ORRERY_LLM_RETRY_INITIAL_DELAY", "") or 1.0)
        maximum = float(os.getenv("ORRERY_LLM_RETRY_MAX_DELAY", "") or 30.0)
    except ValueError as exc:
        raise ValueError(f"Invalid ORRERY_LLM_RETRY_* setting: {exc}") from exc
    if attempts <= 1:
        return None
    return types.HttpRetryOptions(
        attempts=attempts,
        initial_delay=initial,
        max_delay=maximum,
        exp_base=2,
        jitter=1,
        http_status_codes=RETRY_STATUSES,
    )


def is_gemini_model(model: Any) -> bool:
    """Whether *model* (a string, ``BaseLlm`` or chain) is served by Gemini."""
    if isinstance(model, str):
        return True
    if isinstance(model, FallbackLlm):
        return is_gemini_model(model.targets[0])
    from google.adk.models.google_llm import Gemini

    return isinstance(model, Gemini)


def build_fallback_chain(primary: str | BaseLlm, specs: list[str], provider: str) -> FallbackLlm:
    """Wrap *primary* and the models named by *specs* into a :class:`FallbackLlm`.

    Raises:
        ValueError: when the chain mixes Gemini and LiteLLM-routed models.
    """
    if provider == "gemini":
        from google.adk.models.google_llm import Gemini

        if any("/" in spec for spec in specs):
            raise ValueError(
                "MODEL_FALLBACK_CHAIN mixes model families: the primary is Gemini, "
                f"but {specs!r} names a provider-prefixed (LiteLLM) model. ADK "
                "formats tool-call history differently for the two, so a mid-"
                "conversation failover would send history the fallback cannot "
                "pair. Use Gemini models only (e.g. gemini-3.6-flash)."
            )
        head = Gemini(model=primary) if isinstance(primary, str) else primary
        targets: list[BaseLlm] = [head, *(Gemini(model=spec) for spec in specs)]
    else:
        from google.adk.models.lite_llm import LiteLlm

        if any(spec.startswith("gemini") for spec in specs):
            raise ValueError(
                "MODEL_FALLBACK_CHAIN mixes model families: the primary is routed "
                f"through LiteLLM ({provider}), but {specs!r} names a bare Gemini "
                "model. Prefix it with its provider (e.g. vertex_ai/gemini-3.6-flash) "
                "so every link goes through LiteLLM."
            )
        head = primary if isinstance(primary, BaseLlm) else LiteLlm(model=primary)
        targets = [
            head,
            *(LiteLlm(model=spec if "/" in spec else f"{provider}/{spec}") for spec in specs),
        ]
    cooldown = float(os.getenv("MODEL_FALLBACK_COOLDOWN_SECONDS", "") or DEFAULT_COOLDOWN_SECONDS)
    logger.info("Model fallback chain: %s", " -> ".join(t.model for t in targets))
    return FallbackLlm(targets=targets, cooldown_seconds=cooldown)


def resolve_model_chain(primary: str | BaseLlm) -> str | BaseLlm:
    """Return *primary*, or a chain over it when ``MODEL_FALLBACK_CHAIN`` is set.

    ``MODEL_FALLBACK_CHAIN`` is a comma-separated list of models tried in order
    after the primary, in the primary's family: bare Gemini names for a Gemini
    primary, ``provider/model`` (or bare, prefixed with ``MODEL_PROVIDER``) for
    a LiteLLM one. Unset or empty → *primary* unchanged.
    """
    raw = os.getenv("MODEL_FALLBACK_CHAIN", "").strip()
    specs = [spec.strip() for spec in raw.split(",") if spec.strip()]
    if not specs:
        return primary
    provider = os.getenv("MODEL_PROVIDER", "gemini").lower()
    return build_fallback_chain(primary, specs, provider)
