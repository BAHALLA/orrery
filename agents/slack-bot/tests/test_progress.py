"""Live progress in a Slack thread (slack_bot.progress via the real handler)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orrery_core import AgentGateway
from orrery_core.plugins.progress_plugin import ProgressPlugin
from slack_bot import progress as progress_mod
from slack_bot.config import SlackBotConfig
from slack_bot.handler import APP_NAME, SlackAgentHandler


@pytest.fixture(autouse=True)
def fast_refresh(monkeypatch):
    monkeypatch.setattr(progress_mod, "UPDATE_INTERVAL_SECONDS", 0.02)


def _tool(name, *, delegation=False):
    from google.adk.tools.agent_tool import AgentTool

    tool = MagicMock(spec=AgentTool) if delegation else MagicMock()
    tool.name = name
    tool.func = None
    return tool


def _ctx(agent, call_id):
    ctx = MagicMock()
    ctx.agent_name = agent
    ctx.function_call_id = call_id
    return ctx


def _event(text):
    part = MagicMock(text=text, thought=None)
    event = MagicMock(partial=None)
    event.content.parts = [part]
    return event


def _runner(*, delay: float, with_tools: bool = True):
    """A run_async that reports progress through the real ProgressPlugin."""
    plugin = ProgressPlugin()

    async def run_async(**kwargs):
        if with_tools:
            spec, inner = _tool("kafka_health_agent", delegation=True), _tool("get_consumer_lag")
            await plugin.before_tool_callback(
                tool=spec, tool_args={}, tool_context=_ctx("root", "d1")
            )
            await plugin.before_tool_callback(
                tool=inner, tool_args={}, tool_context=_ctx("kafka_health_agent", "t1")
            )
            await asyncio.sleep(delay)
            await plugin.after_tool_callback(
                tool=inner,
                tool_args={},
                tool_context=_ctx("kafka_health_agent", "t1"),
                result={"status": "success"},
            )
            await plugin.after_tool_callback(
                tool=spec, tool_args={}, tool_context=_ctx("root", "d1"), result={"result": "ok"}
            )
        else:
            await asyncio.sleep(delay)
        yield _event("Lag is flat.")

    runner = MagicMock()
    runner.run_async = run_async
    return runner


def _handler(runner, *, progress_updates: bool = True):
    sessions = AsyncMock()
    sessions.create_session = AsyncMock(return_value=MagicMock(id="s1"))
    with patch("orrery_core.serving.gateway.App"), patch("orrery_core.serving.gateway.Runner"):
        gw = AgentGateway(
            app_name=APP_NAME, root_agent=MagicMock(), plugins=[], session_service=sessions
        )
    gw.runner = runner
    from slack_bot.session_map import SessionMap

    config = SlackBotConfig(_env_file=None, slack_progress_updates=progress_updates)
    return SlackAgentHandler(gateway=gw, session_map=SessionMap(), channel_ref={}, config=config)


def _say():
    say = AsyncMock(return_value={"ts": "999.1"})
    say.client = MagicMock()
    say.client.chat_update = AsyncMock()
    say.client.chat_delete = AsyncMock()
    return say


async def _send(handler, say):
    await handler.handle_message(text="lag?", channel="C1", thread_ts="1.0", user_id="U1", say=say)


def _texts(say):
    return [c.kwargs["text"] for c in say.call_args_list]


@pytest.mark.asyncio
async def test_slow_turn_posts_live_status_then_leaves_a_receipt():
    say = _say()

    await _send(_handler(_runner(delay=0.15)), say)

    posted = _texts(say)
    assert posted[0].startswith(":hourglass_flowing_sand: *Working…*")
    assert "*Kafka*" in posted[0] and "`get_consumer_lag`" in posted[0]
    assert posted[-1] == "Lag is flat."  # the answer still arrives, last
    final = say.client.chat_update.call_args.kwargs
    assert final["ts"] == "999.1" and final["channel"] == "C1"
    assert final["text"].startswith(":white_check_mark: Kafka · 1 tool call · ")


@pytest.mark.asyncio
async def test_fast_turn_adds_nothing_but_the_answer():
    say = _say()

    await _send(_handler(_runner(delay=0)), say)

    assert _texts(say) == ["Lag is flat."]
    say.client.chat_update.assert_not_called()


@pytest.mark.asyncio
async def test_slow_turn_without_tools_leaves_no_status_message():
    say = _say()

    await _send(_handler(_runner(delay=0.1, with_tools=False)), say)

    assert _texts(say) == ["Lag is flat."]


@pytest.mark.asyncio
async def test_a_failing_update_never_costs_the_answer():
    say = _say()
    say.client.chat_update = AsyncMock(side_effect=RuntimeError("ratelimited"))

    await _send(_handler(_runner(delay=0.15)), say)

    assert _texts(say)[-1] == "Lag is flat."


@pytest.mark.asyncio
async def test_progress_can_be_turned_off():
    say = _say()

    await _send(_handler(_runner(delay=0.15), progress_updates=False), say)

    assert _texts(say) == ["Lag is flat."]
