"""AEP-031 P0a and P0b: the registry, the composition spec and its refusals."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from orrery_core.security.guardrails import LEVEL_DESTRUCTIVE, set_guard
from orrery_core.studio.config import BudgetCeilings, StudioConfig
from orrery_core.studio.registry import StudioRegistry, load_registry
from orrery_core.studio.spec import (
    SPEC_VERSION,
    CompositionSpec,
    SpecInvalid,
    ValidationContext,
    graph_order,
    parse_spec,
    spec_json_schema,
    validate_spec,
)
from orrery_core.tools.mcp_network import PrivateAllowlist

REGISTRY: dict[str, Any] = {
    "mcp": [
        {
            "id": "docs",
            "url": "https://docs.example.com/mcp",
            "transport": "streamable_http",
            "tools": ["search", "delete_page"],
            "read_only": ["search"],
            "destructive": ["delete_page"],
        }
    ],
    "a2a": [
        {
            "id": "demo-writer",
            "card_url": "https://writer.example.com/.well-known/agent-card.json",
            "skills": ["draft"],
        }
    ],
}


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


def _ctx(**overrides: Any) -> ValidationContext:
    risky = _Tool("restart_service")
    set_guard(risky, LEVEL_DESTRUCTIVE)
    return ValidationContext(
        registry=StudioRegistry.model_validate(REGISTRY),
        catalogue={"get_time": _Tool("get_time"), "restart_service": risky},
        **overrides,
    )


def _node(node_id: str, **extra: Any) -> dict[str, Any]:
    return {"id": node_id, "kind": "llm_agent", "instruction": f"Do {node_id}.", **extra}


def _spec(**overrides: Any) -> dict[str, Any]:
    return {
        "spec_version": SPEC_VERSION,
        "name": "release-notes",
        "nodes": [
            _node("research", tools=["mcp:docs.search", "catalog:get_time"]),
            _node("writer", tools=["a2a:demo-writer.draft"]),
            _node("critic"),
        ],
        "edges": [{"from": "research", "to": "writer"}, {"from": "writer", "to": "critic"}],
        **overrides,
    }


def _errors(raw: dict[str, Any], ctx: ValidationContext | None = None) -> list[dict[str, Any]]:
    try:
        spec = parse_spec(raw)
    except SpecInvalid as exc:
        return [e.as_dict() for e in exc.errors]
    return [e.as_dict() for e in validate_spec(spec, ctx or _ctx())]


def _locs(raw: dict[str, Any], ctx: ValidationContext | None = None) -> list[list[Any]]:
    return [e["loc"] for e in _errors(raw, ctx)]


# ── Specs that run ───────────────────────────────────────────────────


def test_a_three_node_pipeline_is_accepted_in_edge_order():
    raw = _spec()
    raw["nodes"].reverse()  # node order does not matter; edges do
    spec = parse_spec(raw)
    assert validate_spec(spec, _ctx()) == []
    assert graph_order(spec) == ["research", "writer", "critic"]


def test_a_single_node_needs_no_edges():
    spec = parse_spec(_spec(nodes=[_node("only")], edges=[]))
    assert validate_spec(spec, _ctx()) == [] and graph_order(spec) == ["only"]


def test_the_digest_is_stable_and_content_addressed():
    a, b = parse_spec(_spec()), parse_spec(_spec())
    assert a.digest() == b.digest() and len(a.digest()) == 64
    assert parse_spec(_spec(description="changed")).digest() != a.digest()


def test_the_published_schema_names_every_node_kind():
    schema = spec_json_schema()
    text = str(schema)
    for kind in ("llm_agent", "remote_a2a", "router", "parallel", "join", "supervisor"):
        assert kind in text
    assert "from" in str(schema["$defs"]["Edge"]["properties"])


# ── Refusals, field by field ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("kind", "extra", "phase"),
    [
        ("human_approval", {"prompt": "ok?"}, "P0c"),
        ("remote_a2a", {"remote": "demo-writer.draft"}, "P1"),
    ],
)
def test_other_node_kinds_are_not_supported_yet(kind, extra, phase):
    raw = _spec(nodes=[{"id": "x", "kind": kind, **extra}], edges=[])
    [error] = _errors(raw)
    assert error["loc"] == ["nodes", 0, "kind"]
    assert error["code"] == "not_supported_yet" and phase in error["msg"]


def test_the_handoff_pattern_is_not_supported_yet():
    [error] = _errors(_spec(pattern="handoff"))
    assert error["loc"] == ["pattern"] and error["code"] == "not_supported_yet"


def test_a_pattern_must_match_the_graph():
    [error] = _errors(_spec(pattern="supervisor"))
    assert error["loc"] == ["pattern"] and error["code"] == "pattern_mismatch"


def test_a_node_model_must_be_allowed():
    raw = _spec()
    raw["nodes"][2]["model"] = "gpt-x"
    [error] = _errors(raw)
    assert error["loc"] == ["nodes", 2, "model"] and error["code"] == "model_not_allowed"
    assert _errors(raw, _ctx(models=frozenset({"gpt-x"}))) == []


def test_a_last_node_may_be_labelled_and_an_edge_screened():
    raw = _spec()
    raw["nodes"][2]["output"] = {"labels": ["ok", "retry"]}
    raw["edges"][0]["screen"] = True
    assert _errors(raw) == []


@pytest.mark.parametrize(
    ("edge_extra", "field", "fragment"),
    [
        ({"when": "ok"}, "when", "no output.labels"),
        ({"max_iterations": 2}, "max_iterations", "give it when"),
    ],
)
def test_labels_and_loops_need_their_source(edge_extra, field, fragment):
    raw = _spec()
    raw["edges"][0].update(edge_extra)
    errors = _errors(raw)
    assert any(e["loc"] == ["edges", 0, field] and fragment in e["msg"] for e in errors), errors


@pytest.mark.parametrize(
    ("ref", "why"),
    [
        ("search", "not a tool reference"),
        ("mcp:nope.search", "no MCP server 'nope'"),
        ("mcp:docs.missing", "does not list tool 'missing'"),
        ("mcp:docs.delete_page", "not classified read_only"),
        ("a2a:nope.draft", "no remote agent 'nope'"),
        ("a2a:demo-writer.deploy", "not registered for skill 'deploy'"),
        ("catalog:nope", "no catalogue tool 'nope'"),
        ("catalog:restart_service", "changes state"),
    ],
)
def test_bad_tool_refs_are_refused_at_their_index(ref, why):
    raw = _spec()
    raw["nodes"][2]["tools"] = ["catalog:get_time", ref]
    [error] = _errors(raw)
    assert error["loc"] == ["nodes", 2, "tools", 1] and why in error["msg"]


def _named_ctx() -> ValidationContext:
    long_a = "a" * 30 + "-x"
    long_b = "a" * 30 + "_x"
    reg = {
        "mcp": [
            *REGISTRY["mcp"],
            {
                "id": "other",
                "url": "https://o.example.com/mcp",
                "transport": "streamable_http",
                "tools": ["search", "x" * 40],
                "read_only": ["search", "x" * 40],
            },
        ],
        "a2a": [
            *REGISTRY["a2a"],
            {"id": "demo_writer", "card_url": "https://w2.example.com/c", "skills": ["draft"]},
            {"id": long_a, "card_url": "https://l1.example.com/c", "skills": ["s" * 30 + "one"]},
            {"id": long_b, "card_url": "https://l2.example.com/c", "skills": ["s" * 30 + "two"]},
        ],
    }
    time_tool = _Tool("get_time")
    return ValidationContext(
        registry=StudioRegistry.model_validate(reg),
        catalogue={"get_time": time_tool, "clock": time_tool},
    )


@pytest.mark.parametrize(
    ("refs", "code"),
    [
        (["catalog:get_time", "catalog:clock"], "tool_name_collision"),  # one tool, two refs
        (["a2a:demo-writer.draft", "a2a:demo_writer.draft"], "tool_name_collision"),
        (  # both cut to the same 64 characters
            [
                "a2a:" + "a" * 30 + "-x." + "s" * 30 + "one",
                "a2a:" + "a" * 30 + "_x." + "s" * 30 + "two",
            ],
            "tool_name_collision",
        ),
        (["mcp:other." + "x" * 60], None),  # unknown tool: reported as unknown_tool only
        (["mcp:other." + "x" * 40, "catalog:get_time"], None),
    ],
)
def test_tool_names_must_be_unique_per_node(refs, code):
    errors = _errors(_spec(nodes=[_node("only", tools=refs)], edges=[]), _named_ctx())
    if code is None:
        assert all(e["code"] != "tool_name_collision" for e in errors)
    else:
        assert [(e["loc"], e["code"]) for e in errors] == [(["nodes", 0, "tools", 1], code)]


def test_two_nodes_may_share_a_tool_name():
    spec = _spec(
        nodes=[_node("a", tools=["mcp:docs.search"]), _node("b", tools=["mcp:docs.search"])],
        edges=[{"from": "a", "to": "b"}],
    )
    assert _errors(spec) == []


def test_mcp_names_past_64_characters_are_refused():
    ctx = _named_ctx()
    from orrery_core.studio.spec import _name_errors

    long = "y" * 70
    errors = _name_errors(0, ["mcp:other." + long], ctx)
    assert errors == []  # unknown tool: _tool_error's job, not this one
    reg = ctx.registry.model_copy(
        update={
            "mcp": [ctx.registry.mcp[1].model_copy(update={"tools": [long], "read_only": [long]})]
        }
    )
    errors = _name_errors(0, ["mcp:other." + long], ValidationContext(registry=reg))
    assert [e.code for e in errors] == ["tool_name_too_long"]


def test_duplicate_ids_and_tool_refs_are_refused():
    raw = _spec(nodes=[_node("a"), _node("a")], edges=[{"from": "a", "to": "a"}])
    assert ["nodes", 1, "id"] in _locs(raw)
    raw = _spec()
    raw["nodes"][2]["tools"] = ["catalog:get_time", "catalog:get_time"]
    assert _locs(raw) == [["nodes", 2, "tools"]]


@pytest.mark.parametrize(
    ("edges", "loc", "fragment"),
    [
        ([{"from": "research", "to": "ghost"}], ["edges", 0, "to"], "no node 'ghost'"),
        (
            [{"from": "research", "to": "writer"}, {"from": "research", "to": "critic"}],
            ["edges", 1, "from"],
            "only a parallel node fans out",
        ),
        (
            [{"from": "research", "to": "critic"}, {"from": "writer", "to": "critic"}],
            ["edges"],
            "exactly one first node",
        ),
        ([{"from": "writer", "to": "writer"}], ["edges", 0], "loop on one node"),
        (
            [
                {"from": "research", "to": "writer"},
                {"from": "writer", "to": "critic"},
                {"from": "critic", "to": "research"},
            ],
            ["edges"],
            "cycle",
        ),
        ([{"from": "research", "to": "writer"}], ["edges"], "exactly one first node"),
    ],
)
def test_the_edges_must_form_one_chain(edges, loc, fragment):
    errors = _errors(_spec(edges=edges))
    assert any(e["loc"] == loc and fragment in e["msg"] for e in errors), errors


def test_budgets_cannot_exceed_the_deployment_ceilings():
    ctx = _ctx(ceilings=BudgetCeilings(max_tokens=1000))
    [error] = _errors(_spec(budgets={"max_tokens": 5000, "max_tool_calls": 3}), ctx)
    assert error["loc"] == ["budgets", "max_tokens"] and error["code"] == "over_ceiling"
    resolved = parse_spec(_spec(budgets={"max_tool_calls": 3})).budgets.resolved(ctx.ceilings)
    assert resolved.max_tool_calls == 3 and resolved.max_tokens == 1000


def test_schema_errors_point_at_the_field():
    raw = _spec()
    raw["nodes"][0]["instruction"] = ""
    raw["edges"][0]["frm"] = "x"
    locs = _locs(raw)
    assert ["nodes", 0, "instruction"] in locs  # no "llm_agent" tag in the path
    assert ["edges", 0, "frm"] in locs


@pytest.mark.parametrize(
    ("version", "fragment"),
    [(SPEC_VERSION + 1, "newer than this server"), ("1", "must be an integer"), (0, "not known")],
)
def test_spec_versions(version, fragment):
    [error] = _errors(_spec(spec_version=version))
    assert error["loc"] == ["spec_version"] and fragment in error["msg"]


def test_unknown_keys_are_refused_so_typos_do_not_pass():
    assert ["nodes", 0, "tool"] in _locs(_spec(nodes=[_node("a", tool=["x"])], edges=[]))


def test_a_spec_is_not_executable_code():
    # Instructions are text: nothing in the format names a module, file or command.
    props = str(CompositionSpec.model_json_schema())
    for word in ("command", "module", "import", "path", "url"):
        assert f"'{word}'" not in props


# ── Registry ─────────────────────────────────────────────────────────


def test_a2a_acting_entries_are_refused():
    raw = {"a2a": [{**REGISTRY["a2a"][0], "effect": "acting"}]}
    with pytest.raises(ValidationError, match="acting is not supported yet"):
        StudioRegistry.model_validate(raw)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("command", "rm"),
        ("headers_from_env", {"Authorization": "TOKEN"}),
        ("ca_file", "/etc/ca.pem"),
        ("client_cert_file", "/c.pem"),
        ("allow_insecure_http", True),
        ("name", "docs"),
    ],
)
def test_mcp_entries_refuse_stdio_env_headers_and_files(key, value):
    raw = {"mcp": [{**REGISTRY["mcp"][0], key: value}]}
    with pytest.raises(ValidationError, match=f"`{key}`"):
        StudioRegistry.model_validate(raw)


def test_mcp_entries_refuse_stdio_and_off_loopback_http():
    with pytest.raises(ValidationError, match="stdio"):
        StudioRegistry.model_validate({"mcp": [{"id": "x", "transport": "stdio", "tools": ["t"]}]})
    with pytest.raises(ValidationError, match="https"):
        StudioRegistry.model_validate(
            {"mcp": [{**REGISTRY["mcp"][0], "url": "http://docs.example.com/mcp"}]}
        )
    ok = StudioRegistry.model_validate(
        {"mcp": [{**REGISTRY["mcp"][0], "url": "http://127.0.0.1:9000/mcp"}]}
    )
    assert ok.mcp_entry("docs") is not None


def test_a_tool_both_read_only_and_destructive_is_refused():
    raw = {"mcp": [{**REGISTRY["mcp"][0], "read_only": ["search", "delete_page"]}]}
    with pytest.raises(ValidationError, match="both read_only and destructive"):
        StudioRegistry.model_validate(raw)


def test_mcp_entries_are_the_mcp_config_schema():
    server = StudioRegistry.model_validate(REGISTRY).mcp_entry("docs")
    assert server is not None
    assert type(server).__name__ == "McpServerConfig"
    assert server.name == "docs" and server.description == "MCP server docs"


def test_duplicate_registry_ids_are_refused():
    with pytest.raises(ValidationError, match="duplicate a2a ids"):
        StudioRegistry.model_validate({"a2a": [REGISTRY["a2a"][0]] * 2})


def test_private_entries_must_be_on_the_deployment_allowlist(tmp_path: Path):
    import yaml

    private = {
        **REGISTRY["a2a"][0],
        "card_url": "https://writer.internal/card",
        "private_network": True,
    }
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump({"a2a": [private]}))
    from orrery_core.a2a.remote import RemoteNetwork

    with pytest.raises(ValueError, match="ORRERY_A2A_PRIVATE_ALLOW"):
        load_registry(path, network=RemoteNetwork(private_allow=PrivateAllowlist()))
    # A literal address is checked at load; a hostname may be covered by a
    # listed network, so it is checked against what it resolves to per request.
    literal = {**private, "card_url": "https://10.9.0.4/card"}
    path.write_text(yaml.safe_dump({"a2a": [literal]}))
    with pytest.raises(ValueError, match="not in ORRERY_A2A_PRIVATE_ALLOW"):
        load_registry(
            path, network=RemoteNetwork(private_allow=PrivateAllowlist.parse("x.internal"))
        )
    assert load_registry(
        path, network=RemoteNetwork(private_allow=PrivateAllowlist.parse("10.9.0.0/16"))
    )
    path.write_text(yaml.safe_dump({"a2a": [private]}))
    ok = load_registry(
        path, network=RemoteNetwork(private_allow=PrivateAllowlist.parse("writer.internal"))
    )
    entry = ok.a2a_entry("demo-writer")
    assert entry is not None and entry.private_network is True


def test_a_missing_secret_fails_loading(tmp_path: Path, monkeypatch):
    import yaml

    authed = {**REGISTRY["a2a"][0], "auth": {"bearer_env": "ORRERY_STUDIO_SECRET_WRITER"}}
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump({"a2a": [authed]}))
    monkeypatch.delenv("ORRERY_STUDIO_SECRET_WRITER", raising=False)
    with pytest.raises(ValueError, match="ORRERY_STUDIO_SECRET_WRITER"):
        load_registry(path)
    monkeypatch.setenv("ORRERY_STUDIO_SECRET_WRITER", "t")
    entry = load_registry(path).a2a_entry("demo-writer")
    assert entry is not None and entry.auth is not None


def test_no_registry_configured_is_empty_and_a_missing_file_fails(monkeypatch, tmp_path):
    monkeypatch.delenv("ORRERY_STUDIO_REGISTRY", raising=False)
    assert load_registry() == StudioRegistry()
    monkeypatch.setenv("ORRERY_STUDIO_REGISTRY", str(tmp_path / "nope.yaml"))
    with pytest.raises(FileNotFoundError):
        load_registry()


TEMPLATES = Path(__file__).resolve().parents[2] / "deploy" / "studio-demo" / "specs"
#: Template file -> the pattern it shows.
TEMPLATE_PATTERNS = {
    "pipeline": "pipeline",
    "router": "router",
    "critic-loop": "critic_loop",
    "fan-out-fan-in": "fan_out_fan_in",
    "supervisor": "supervisor",
    "debate": "debate",
    "map-reduce": "map_reduce",
    "plan-and-execute": "plan_and_execute",
}


def _demo_registry(monkeypatch) -> StudioRegistry:
    import yaml

    monkeypatch.setenv("ORRERY_A2A_PRIVATE_ALLOW", "demo-writer.studio.internal")
    root = TEMPLATES.parent
    registry = StudioRegistry.model_validate(yaml.safe_load((root / "registry.yaml").read_text()))
    registry.check_network(PrivateAllowlist.from_env())
    return registry


def test_there_is_a_template_for_each_pattern_the_server_runs():
    assert sorted(p.stem for p in TEMPLATES.glob("*.json")) == sorted(TEMPLATE_PATTERNS)


@pytest.mark.parametrize("name", sorted(TEMPLATE_PATTERNS))
def test_each_template_is_valid_against_the_demo_registry_and_compiles(name, monkeypatch):
    import json

    from orrery_core.a2a.remote import RemoteCards
    from orrery_core.studio.compiler import compile_spec

    registry = _demo_registry(monkeypatch)
    spec = parse_spec(json.loads((TEMPLATES / f"{name}.json").read_text()))
    assert spec.pattern == TEMPLATE_PATTERNS[name]
    assert validate_spec(spec, ValidationContext(registry=registry)) == []
    compiled = compile_spec(
        spec, registry=registry, cards=RemoteCards(), catalogue={}, model="gemini-3.6-flash"
    )
    try:
        agents = {info.node_id for info in compiled.nodes.values()}
        assert agents == {n.id for n in spec.nodes if n.kind in ("llm_agent", "supervisor")}
    finally:
        import asyncio

        asyncio.run(compiled.close())


# ── Config ───────────────────────────────────────────────────────────


def test_the_studio_refuses_to_start_without_auth_or_human_clients():
    config = StudioConfig(enabled=True)
    with pytest.raises(ValueError, match="ORRERY_HUMAN_CLIENTS"):
        config.validate(auth_enabled=True, human_clients_configured=False)
    with pytest.raises(ValueError, match="AUTH_ENABLED"):
        config.validate(auth_enabled=False, human_clients_configured=True)
    config.validate(auth_enabled=True, human_clients_configured=True)


def test_ceilings_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("ORRERY_STUDIO_ENABLED", "true")
    monkeypatch.setenv("ORRERY_STUDIO_MAX_TOKENS", "1234")
    config = StudioConfig.from_env()
    assert config.enabled and config.ceilings.max_tokens == 1234


# ── P0b: the graph ───────────────────────────────────────────────────


def _graph(nodes: list[dict], edges: list[dict], pattern: str = "pipeline", **extra: Any):
    return _spec(nodes=nodes, edges=edges, pattern=pattern, **extra)


def _labelled(node_id: str, *labels: str) -> dict[str, Any]:
    return _node(node_id, output={"labels": list(labels or ("yes", "no"))})


def _fan(n: int) -> dict[str, Any]:
    branches = [f"b{i}" for i in range(n)]
    return _graph(
        [
            _node("start"),
            {"id": "fan", "kind": "parallel"},
            *(_node(b) for b in branches),
            {"id": "meet", "kind": "join"},
            _node("end"),
        ],
        [
            {"from": "start", "to": "fan"},
            *({"from": "fan", "to": b} for b in branches),
            *({"from": b, "to": "meet"} for b in branches),
            {"from": "meet", "to": "end"},
        ],
        "fan_out_fan_in",
    )


def _critic_loop(**edge: Any) -> dict[str, Any]:
    return _graph(
        [_node("writer"), _labelled("critic", "approved", "rejected")],
        [
            {"from": "writer", "to": "critic"},
            {"from": "critic", "to": "writer", "when": "rejected", **edge},
        ],
        "critic_loop",
    )


def _codes(raw: dict[str, Any], ctx: ValidationContext | None = None) -> list[str]:
    return [e["code"] for e in _errors(raw, ctx)]


def test_a_fan_out_of_six_runs_and_seven_is_refused():
    assert _errors(_fan(6)) == []
    assert "too_many_branches" in _codes(_fan(7))


def test_a_loop_needs_a_cap_of_at_most_five():
    assert _errors(_critic_loop(max_iterations=5)) == []
    assert _codes(_critic_loop()) == ["unbounded_cycle"]
    assert _locs(_critic_loop(max_iterations=6)) == [["edges", 1, "max_iterations"]]


def test_max_iterations_is_only_for_an_edge_back():
    raw = _graph(
        [_labelled("a"), _node("b")],
        [{"from": "a", "to": "b", "when": "yes", "max_iterations": 2}],
        "router",
    )
    assert any("edge back to an earlier node" in e["msg"] for e in _errors(raw))


def test_edges_on_labels_must_use_the_source_labels_once_each():
    raw = _graph(
        [_labelled("a", "yes", "no"), _node("b"), _node("c")],
        [{"from": "a", "to": "b", "when": "maybe"}, {"from": "a", "to": "c", "when": "yes"}],
        "router",
    )
    assert any("not one of 'a'" in e["msg"] for e in _errors(raw))
    raw["edges"][0]["when"] = "yes"
    assert any("one per label" in e["msg"] for e in _errors(raw))
    raw["edges"][0].pop("when")
    assert any("each of its edges needs when" in e["msg"] for e in _errors(raw))


def test_a_router_follows_one_labelled_node():
    raw = _graph(
        [_node("a"), {"id": "r", "kind": "router"}, _node("b")],
        [{"from": "a", "to": "r"}, {"from": "r", "to": "b", "when": "yes"}],
        "router",
    )
    assert any(
        "router follows exactly one node with output.labels" in e["msg"] for e in _errors(raw)
    )


def test_a_join_closes_every_branch_of_one_parallel_node():
    raw = _fan(3)
    raw["edges"] = [e for e in raw["edges"] if e != {"from": "b2", "to": "meet"}]
    raw["edges"].append({"from": "b2", "to": "end"})
    assert any("must meet at one join" in e["msg"] or "ends at" in e["msg"] for e in _errors(raw))
    no_after = _fan(2)
    no_after["edges"] = [e for e in no_after["edges"] if e["from"] != "meet"]
    no_after["nodes"] = [n for n in no_after["nodes"] if n["id"] != "end"]
    assert any("needs one node after it" in e["msg"] for e in _errors(no_after))


def test_a_branch_does_not_route():
    raw = _fan(2)
    raw["nodes"][2]["output"] = {"labels": ["yes", "no"]}
    raw["edges"] = [
        {**e, "when": "yes"} if e == {"from": "b0", "to": "meet"} else e for e in raw["edges"]
    ]
    assert any("neither routes nor loops" in e["msg"] for e in _errors(raw))


@pytest.mark.parametrize("target", ["b0", "fan", "start"])
def test_a_loop_cannot_contain_a_parallel_section(target):
    # A join does not start over: entered again, it would fire on the earlier
    # round's answers (seen as nodes after it running twice per round).
    raw = _fan(2)
    raw["nodes"][-1] = _labelled("end", "done", "again")
    raw["edges"].append({"from": "end", "to": target, "when": "again", "max_iterations": 2})
    [error] = _errors(raw)
    assert error["code"] == "loop_around_parallel" and error["loc"] == ["edges", 6]


def test_a_loop_after_the_join_is_fine():
    raw = _fan(2)
    raw["nodes"][-1] = _labelled("end", "done", "again")
    raw["nodes"].insert(-1, _node("draft"))
    raw["edges"][-1] = {"from": "meet", "to": "draft"}
    raw["edges"].append({"from": "draft", "to": "end"})
    raw["edges"].append({"from": "end", "to": "draft", "when": "again", "max_iterations": 2})
    assert _errors(raw) == []


def test_a_branch_does_not_fan_out_again():
    raw = _graph(
        [
            {"id": "outer", "kind": "parallel"},
            {"id": "inner", "kind": "parallel"},
            _node("x"),
            _node("y"),
            {"id": "inner_join", "kind": "join"},
            _node("z"),
            {"id": "outer_join", "kind": "join"},
            _node("end"),
        ],
        [
            {"from": "outer", "to": "inner"},
            {"from": "inner", "to": "x"},
            {"from": "inner", "to": "y"},
            {"from": "x", "to": "inner_join"},
            {"from": "y", "to": "inner_join"},
            {"from": "inner_join", "to": "outer_join"},
            {"from": "outer", "to": "z"},
            {"from": "z", "to": "outer_join"},
            {"from": "outer_join", "to": "end"},
        ],
        "fan_out_fan_in",
    )
    assert any("does not fan out again" in e["msg"] for e in _errors(raw))


def _supervisor(node_id: str, *children: str) -> dict[str, Any]:
    return {"id": node_id, "kind": "supervisor", "instruction": "Lead.", "children": list(children)}


def test_supervisors_nest_three_deep_and_no_more():
    three = _graph(
        [
            _supervisor("s1", "s2"),
            _supervisor("s2", "s3"),
            _supervisor("s3", "leaf"),
            _node("leaf"),
        ],
        [],
        "supervisor",
    )
    assert _errors(three) == []
    four = _graph(
        [
            _supervisor("s0", "s1"),
            _supervisor("s1", "s2"),
            _supervisor("s2", "s3"),
            _supervisor("s3", "leaf"),
            _node("leaf"),
        ],
        [],
        "supervisor",
    )
    assert "too_deep" in _codes(four)


def test_a_child_is_a_tool_not_a_step():
    raw = _graph(
        [_supervisor("lead", "helper"), _node("helper"), _node("after")],
        [{"from": "lead", "to": "after"}, {"from": "helper", "to": "after"}],
        "supervisor",
    )
    assert any("children are its tools" in e["msg"] for e in _errors(raw))
    labelled = _graph([_supervisor("lead", "helper"), _labelled("helper")], [], "supervisor")
    assert any("cannot have output labels" in e["msg"] for e in _errors(labelled))
    twice = _graph(
        [_supervisor("a", "helper"), _supervisor("b", "helper"), _node("helper")],
        [{"from": "a", "to": "b"}],
        "supervisor",
    )
    assert any("already a child of 'a'" in e["msg"] for e in _errors(twice))


def test_a_child_may_not_shadow_a_tool_of_its_supervisor():
    ctx = ValidationContext(
        registry=StudioRegistry.model_validate(REGISTRY), catalogue={"n_helper": _Tool("n_helper")}
    )
    raw = _graph(
        [{**_supervisor("lead", "helper"), "tools": ["catalog:n_helper"]}, _node("helper")],
        [],
        "supervisor",
    )
    assert "tool_name_collision" in _codes(raw, ctx)


def test_graph_order_is_breadth_first_then_children():
    spec = parse_spec(
        _graph(
            [_node("helper"), _supervisor("lead", "helper"), _node("first")],
            [{"from": "first", "to": "lead"}],
            "supervisor",
        )
    )
    assert graph_order(spec) == ["first", "lead", "helper"]


def test_spec_version_stays_one_and_p0a_specs_still_parse():
    assert SPEC_VERSION == 1
    assert _errors(_spec()) == []
