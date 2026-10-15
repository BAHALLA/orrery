"""The Chat progress card shows work done inside specialists (ProgressPlugin feed)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from google.adk.tools.agent_tool import AgentTool
from google_chat_bot.cards import build_progress_card
from google_chat_bot.progress import ProgressTracker

from orrery_core.plugins.progress_plugin import ProgressPlugin, progress_scope


def _tool(name, *, delegation=False):
    tool = MagicMock(spec=AgentTool) if delegation else MagicMock()
    tool.name = name
    tool.func = None
    return tool


def _ctx(agent, call_id):
    return MagicMock(agent_name=agent, function_call_id=call_id)


@pytest.mark.asyncio
async def test_specialist_tool_calls_reach_the_card_while_the_root_is_silent():
    """No runner event arrives during a delegation; the plugin feed must flush."""
    updates: list[list[str]] = []

    async def on_update(t: ProgressTracker) -> None:
        updates.append(t.specialist_lines())

    tracker = ProgressTracker(on_update=on_update)
    plugin = ProgressPlugin()
    spec, inner = _tool("kafka_health_agent", delegation=True), _tool("get_consumer_lag")

    with progress_scope(tracker.sink):
        await plugin.before_tool_callback(tool=spec, tool_args={}, tool_context=_ctx("root", "d1"))
        await plugin.before_tool_callback(
            tool=inner, tool_args={}, tool_context=_ctx("kafka_health_agent", "t1")
        )
        await plugin.after_tool_callback(
            tool=inner,
            tool_args={},
            tool_context=_ctx("kafka_health_agent", "t1"),
            result={"status": "success"},
        )
        await plugin.after_tool_callback(
            tool=spec, tool_args={}, tool_context=_ctx("root", "d1"), result={"result": "ok"}
        )
    await asyncio.sleep(0)  # let scheduled flushes run

    assert updates, "the card was never updated from the plugin feed"
    last = updates[-1][0]
    assert "<b>Kafka</b>" in last and "<i>get_consumer_lag</i>" in last
    assert last.startswith("✅")  # the delegation finished


def test_card_renders_the_specialist_section():
    card = build_progress_card(
        current_agent="orrery_chat_agent",
        current_tool=None,
        subsystem_chips={},
        specialists=["⏳ <b>Kubernetes</b> — ⏳ <i>list_pods</i>"],
    )

    texts = [
        w.get("textParagraph", {}).get("text", "") for w in card["card"]["sections"][0]["widgets"]
    ]
    assert any("<b>Kubernetes</b>" in t for t in texts)


def test_card_without_specialists_is_unchanged():
    card = build_progress_card(current_agent=None, current_tool=None, subsystem_chips={})

    assert not any("divider" in w for w in card["card"]["sections"][0]["widgets"])
