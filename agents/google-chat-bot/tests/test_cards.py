"""Tests for the Google Chat card builders."""

from google_chat_bot.cards import build_confirmation_card

from ai_agents_core import LEVEL_CONFIRM, LEVEL_DESTRUCTIVE


def test_confirmation_card_structure():
    card = build_confirmation_card(
        tool_name="restart_pod",
        args={"name": "api", "ns": "prod"},
        reason="restarts a pod",
        level=LEVEL_CONFIRM,
        action_id="abc123",
    )
    assert card["cardId"] == "abc123"

    header = card["card"]["header"]
    assert "restart_pod" in header["title"]
    assert header["subtitle"] == "Safety Guardrail"

    widgets = card["card"]["sections"][0]["widgets"]
    reason_widget = next(
        w for w in widgets if "Reason" in w.get("textParagraph", {}).get("text", "")
    )
    assert "restarts a pod" in reason_widget["textParagraph"]["text"]

    args_widget = next(
        w for w in widgets if "Arguments" in w.get("textParagraph", {}).get("text", "")
    )
    assert "name=api" in args_widget["textParagraph"]["text"]
    assert "ns=prod" in args_widget["textParagraph"]["text"]

    buttons = next(w for w in widgets if "buttonList" in w)["buttonList"]["buttons"]
    names = {b["text"]: b for b in buttons}
    assert set(names) == {"Approve", "Deny"}

    approve_params = names["Approve"]["onClick"]["action"]["parameters"]
    assert {"key": "action_id", "value": "abc123"} in approve_params
    assert names["Approve"]["onClick"]["action"]["function"] == "confirm_action"
    assert names["Deny"]["onClick"]["action"]["function"] == "deny_action"


def test_destructive_card_uses_warning_emoji():
    card = build_confirmation_card(
        tool_name="drop_topic",
        args={},
        reason="deletes Kafka data",
        level=LEVEL_DESTRUCTIVE,
        action_id="xyz",
    )
    title = card["card"]["header"]["title"]
    assert title.startswith("\u26a0")  # warning sign
    assert "DESTRUCTIVE" in title


def test_card_handles_empty_args():
    card = build_confirmation_card(
        tool_name="list_pods",
        args={},
        reason="",
        level=LEVEL_CONFIRM,
        action_id="id1",
    )
    widgets = card["card"]["sections"][0]["widgets"]
    # No reason widget when reason is empty.
    assert not any("Reason" in w.get("textParagraph", {}).get("text", "") for w in widgets)
    # Args widget still present with "none" placeholder.
    args_widget = next(
        w for w in widgets if "Arguments" in w.get("textParagraph", {}).get("text", "")
    )
    assert "none" in args_widget["textParagraph"]["text"]
