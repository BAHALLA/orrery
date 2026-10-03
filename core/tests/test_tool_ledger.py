"""Tests for ToolLedgerPlugin — executed mutations and refusals of one run."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from google.adk.tools import FunctionTool

from orrery_core import confirm, destructive
from orrery_core.plugins import ToolLedger, ToolLedgerPlugin, current_tool_ledger
from orrery_core.plugins.tool_ledger_plugin import tool_ledger_scope


@destructive("deletes a thing")
async def delete_thing(name: str) -> dict:
    return {"status": "success"}


@confirm("scales a thing")
async def scale_thing(name: str) -> dict:
    return {"status": "success"}


async def read_thing(name: str) -> dict:
    return {"status": "success"}


def _ctx(call_id: str) -> Any:
    ctx = MagicMock()
    ctx.function_call_id = call_id
    return ctx


async def _call(plugin: ToolLedgerPlugin, tool: FunctionTool, call_id: str, result: dict) -> None:
    ctx = _ctx(call_id)
    await plugin.before_tool_callback(tool=tool, tool_args={}, tool_context=ctx)
    await plugin.after_tool_callback(tool=tool, tool_args={}, tool_context=ctx, result=result)


@pytest.mark.asyncio
async def test_unarmed_plugin_is_inert():
    plugin = ToolLedgerPlugin()
    assert current_tool_ledger() is None
    # Nothing to record into, and nothing raised.
    await _call(plugin, FunctionTool(delete_thing), "c1", {"status": "success"})
    assert current_tool_ledger() is None


@pytest.mark.asyncio
async def test_executed_guarded_calls_are_mutations_reads_are_not():
    plugin = ToolLedgerPlugin()
    with tool_ledger_scope() as ledger:
        await _call(plugin, FunctionTool(read_thing), "c1", {"status": "success"})
        await _call(plugin, FunctionTool(scale_thing), "c2", {"status": "success"})
        await _call(plugin, FunctionTool(delete_thing), "c3", {"status": "error"})
    assert ledger.calls() == ["read_thing", "scale_thing", "delete_thing"]
    # A failed guarded call still counts: "it failed" is not "it changed nothing".
    assert ledger.mutations() == ["scale_thing", "delete_thing"]
    assert ledger.refusals() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["BLOCKED", "AWAITING_CONFIRMATION", "access_denied", "confirmation_required"]
)
async def test_gate_answers_are_refusals_not_mutations(status):
    plugin = ToolLedgerPlugin()
    with tool_ledger_scope() as ledger:
        await _call(plugin, FunctionTool(delete_thing), "c1", {"status": status})
    assert ledger.mutations() == []
    assert ledger.refusals() == [("delete_thing", status)]
    assert ledger.in_flight == {}


@pytest.mark.asyncio
async def test_call_cut_short_mid_tool_counts_as_a_possible_mutation():
    """Started, never finished: the effect is unknown, so it is reported."""
    plugin = ToolLedgerPlugin()
    with tool_ledger_scope() as ledger:
        await plugin.before_tool_callback(
            tool=FunctionTool(delete_thing), tool_args={}, tool_context=_ctx("c1")
        )
    assert ledger.mutations() == ["delete_thing"]


@pytest.mark.asyncio
async def test_child_task_appends_into_the_callers_ledger():
    """A sub-run in its own task (as AgentTool's may be) records into the caller's ledger.

    ``create_task`` copies the context, so the child sees the same ledger
    object; mutating it is what makes the record visible to the caller.
    """
    plugin = ToolLedgerPlugin()
    with tool_ledger_scope() as ledger:
        await asyncio.create_task(
            _call(plugin, FunctionTool(scale_thing), "c1", {"status": "success"})
        )
    assert ledger.mutations() == ["scale_thing"]


@pytest.mark.asyncio
async def test_scope_accepts_a_prebuilt_ledger_and_restores_the_previous_one():
    mine = ToolLedger()
    with tool_ledger_scope(mine) as armed:
        assert armed is mine
        assert current_tool_ledger() is mine
    assert current_tool_ledger() is None
