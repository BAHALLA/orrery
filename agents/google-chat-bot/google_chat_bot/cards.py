"""Helpers to build Google Chat Cards v2 for interactive elements."""

from __future__ import annotations

from typing import Any

from ai_agents_core import LEVEL_DESTRUCTIVE


def build_confirmation_card(
    tool_name: str,
    args: dict[str, Any],
    reason: str,
    level: str,
    action_id: str,
) -> dict[str, Any]:
    """Build a single Google Chat Card v2 entry for a tool confirmation.

    Returns a ``{"cardId", "card"}`` dict suitable for inclusion in a
    ``cardsV2`` array. The handler merges multiple entries into the final
    synchronous webhook response.
    """
    emoji = "\u26a0\ufe0f" if level == LEVEL_DESTRUCTIVE else "\U0001f535"
    level_label = "DESTRUCTIVE" if level == LEVEL_DESTRUCTIVE else "Confirmation Required"

    header_text = f"{emoji} {level_label}: {tool_name}"
    args_text = ", ".join(f"<i>{k}={v}</i>" for k, v in args.items()) if args else "<i>none</i>"

    widgets: list[dict[str, Any]] = []
    if reason:
        widgets.append({"textParagraph": {"text": f"<b>Reason:</b> {reason}"}})
    widgets.append({"textParagraph": {"text": f"<b>Arguments:</b> {args_text}"}})
    widgets.append(
        {
            "buttonList": {
                "buttons": [
                    {
                        "text": "Approve",
                        "onClick": {
                            "action": {
                                "function": "confirm_action",
                                "parameters": [{"key": "action_id", "value": action_id}],
                            }
                        },
                        "color": {"red": 0.1, "green": 0.6, "blue": 0.1},
                    },
                    {
                        "text": "Deny",
                        "onClick": {
                            "action": {
                                "function": "deny_action",
                                "parameters": [{"key": "action_id", "value": action_id}],
                            }
                        },
                        "color": {"red": 0.8, "green": 0.1, "blue": 0.1},
                    },
                ]
            }
        }
    )

    return {
        "cardId": action_id,
        "card": {
            "header": {"title": header_text, "subtitle": "Safety Guardrail"},
            "sections": [{"widgets": widgets}],
        },
    }
