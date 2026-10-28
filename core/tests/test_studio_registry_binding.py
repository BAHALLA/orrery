"""``check_deployment_bound``: an edit may only tighten an entry the deployment binds."""

from __future__ import annotations

from typing import Any

import pytest

from orrery_core.studio.registry import check_deployment_bound

A2A = {
    "id": "writer",
    "card_url": "https://writer.internal/.well-known/agent-card.json",
    "card_sha256": "a" * 64,
    "skills": ["draft", "review"],
    "effect": "acting",
    "private_network": True,
    "auth": {"bearer_env": "ORRERY_STUDIO_SECRET_WRITER"},
}
MCP = {
    "id": "ops",
    "url": "https://ops.internal/mcp",
    "transport": "streamable_http",
    "private_network": True,
    "tools": ["get", "list", "restart"],
    "read_only": ["get", "list"],
    "destructive": ["restart"],
    "pinned": {"get": "b" * 64},
}
FILE = {"a2a": [A2A], "mcp": [MCP]}


def _check(a2a: dict[str, Any] | None = None, mcp: dict[str, Any] | None = None) -> None:
    check_deployment_bound({"a2a": [a2a or A2A], "mcp": [mcp or MCP]}, FILE)


def test_the_file_itself_and_tighter_edits_pass() -> None:
    _check()
    _check(a2a={**A2A, "skills": ["draft"], "description": "x"})
    _check(mcp={**MCP, "tools": ["get"], "read_only": ["get"], "destructive": []})
    _check(mcp={**MCP, "read_only": [], "destructive": ["restart", "list"]})


@pytest.mark.parametrize(
    ("a2a", "why"),
    [
        ({**A2A, "effect": "read_only"}, "effect: acting"),
        ({k: v for k, v in A2A.items() if k != "effect"}, "effect: acting"),
        ({**A2A, "skills": ["draft", "deploy"]}, "skills added"),
        ({**A2A, "card_sha256": None}, "card_sha256"),
        ({**A2A, "card_sha256": "c" * 64}, "card_sha256"),
    ],
)
def test_a_bound_a2a_entry_cannot_be_loosened(a2a: dict[str, Any], why: str) -> None:
    with pytest.raises(ValueError, match="only be made stricter") as exc:
        _check(a2a=a2a)
    assert why in str(exc.value)


@pytest.mark.parametrize(
    ("mcp", "why"),
    [
        ({**MCP, "tools": [*MCP["tools"], "delete"]}, "tools added"),
        ({**MCP, "read_only": ["get", "list", "restart"]}, "read_only added"),
        ({**MCP, "destructive": []}, "destructive dropped"),
        ({**MCP, "trust_annotations": True}, "trust_annotations"),
        ({**MCP, "pinned": {}}, "pins changed"),
        ({**MCP, "pinned": {"get": "d" * 64}}, "pins changed"),
    ],
)
def test_a_bound_mcp_entry_cannot_be_loosened(mcp: dict[str, Any], why: str) -> None:
    with pytest.raises(ValueError, match="only be made stricter") as exc:
        _check(mcp=mcp)
    assert why in str(exc.value)


def test_a_pin_of_a_tool_no_longer_exposed_may_go() -> None:
    _check(mcp={**MCP, "tools": ["list"], "read_only": ["list"], "pinned": {}})


def test_any_one_file_entry_on_the_endpoint_is_enough() -> None:
    lax = {**A2A, "id": "writer-lax", "effect": "read_only", "card_sha256": None}
    check_deployment_bound(
        {"a2a": [{**A2A, "effect": "read_only", "card_sha256": None}]}, {"a2a": [A2A, lax]}
    )


def test_an_unbound_public_entry_is_the_admins_to_class() -> None:
    public = {"id": "pub", "card_url": "https://pub.example/card.json", "skills": ["x"]}
    check_deployment_bound({"a2a": [public]}, {})
