"""The models Studio nodes may name (``ORRERY_STUDIO_MODELS``), built at startup.

Each name is built once, with :func:`~orrery_core.agent.base.resolve_model`
(one model, never the fallback chain), so a typo, a missing ``litellm`` or a
missing provider key fails startup instead of the first run that uses it.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from typing import Any

from ..agent.base import resolve_model
from .config import STUDIO_MODELS_ENV

_TRUE = {"1", "true", "yes", "on"}


def _missing_keys(name: str) -> list[str]:
    """Credentials *name* needs that the environment lacks (best effort)."""
    if "/" not in name:  # Gemini: an API key, or Vertex AI (ADC)
        if os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").strip().lower() in _TRUE:
            return []
        if os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"):
            return []
        return ["GOOGLE_API_KEY (or GOOGLE_GENAI_USE_VERTEXAI=true)"]
    import litellm

    found = litellm.validate_environment(model=name)
    return list(found.get("missing_keys") or [])


def build_models(names: Iterable[str]) -> dict[str, Any]:
    """Each allowlisted model, built. Raises ``ValueError`` naming the bad one."""
    built: dict[str, Any] = {}
    for name in sorted(names):
        try:
            model = resolve_model(name)
            missing = _missing_keys(name)
        except ImportError as exc:
            raise ValueError(
                f"{STUDIO_MODELS_ENV}: {name!r} is routed through LiteLLM, which is not "
                f"installed ({exc})"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - any failure to build is a bad entry
            raise ValueError(f"{STUDIO_MODELS_ENV}: {name!r} cannot be built: {exc}") from exc
        if missing:
            raise ValueError(f"{STUDIO_MODELS_ENV}: {name!r} needs {', '.join(missing)} set")
        built[name] = model
    return built


def model_lookup(names: Iterable[str]) -> Callable[[str], Any]:
    """``model_for`` for :class:`~orrery_core.studio.runner.StudioService`."""
    built = build_models(names)

    def model_for(name: str) -> Any:
        return built[name]  # validation admits only allowlisted names

    return model_for
