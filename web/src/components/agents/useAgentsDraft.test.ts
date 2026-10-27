import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { CatalogResponse, InventoryAgent } from "../../api/types";
import { useAgentsDraft, type AgentsDraftOptions } from "./useAgentsDraft";

const triager = {
  name: "triager",
  description: "Sweeps the pods",
  instruction: "Look at the pods.",
  tools: ["list_pods"],
};

/** An editable catalog: docker switched off, k8s on, one composed agent. */
const catalog: CatalogResponse = {
  editing_enabled: true,
  applied_version: 3,
  catalog: {
    version: 3,
    disabled_specialists: ["docker"],
    agents: [triager],
    updated_by: "alice",
    updated_at: 0,
  },
  specialists: [
    {
      key: "docker",
      agent_name: "docker_agent",
      description: "Containers",
      source: "builtin",
      available: true,
    },
    {
      key: "k8s",
      agent_name: "k8s_health_agent",
      description: "Kubernetes",
      source: "builtin",
      available: true,
    },
    { key: "es", agent_name: "es_agent", description: "", source: "builtin", available: false },
  ],
  tools: [
    { ref: "list_containers", specialist: "docker", guard: "read", reason: "" },
    { ref: "list_pods", specialist: "k8s", guard: "read", reason: "" },
    { ref: "scale_deployment", specialist: "k8s", guard: "confirm", reason: "scales" },
  ],
  file_agents: ["from_file"],
  reserved_names: ["orrery_chat_agent"],
};

/** What the server runs: k8s, the composed agent, and an MCP agent. */
const agents: InventoryAgent[] = [
  {
    name: "k8s_health_agent",
    description: "Kubernetes",
    source: "builtin",
    model: "gemini-3-pro",
    tools: [
      { name: "list_pods", guard: "read", reason: "" },
      { name: "scale_deployment", guard: "confirm", reason: "scales" },
    ],
  },
  { name: "triager", description: "", source: "declarative", tools: [] },
  {
    name: "github_mcp_agent",
    description: "GitHub",
    source: "mcp",
    tools: [{ name: "github_*", guard: "dynamic", reason: "" }],
  },
];

function setup(over: Partial<AgentsDraftOptions> = {}) {
  const options: AgentsDraftOptions = {
    agents,
    catalog,
    carried: null,
    onDirtyChange: vi.fn(),
    onDiscard: vi.fn(),
    onSave: vi.fn(async () => {}),
    ...over,
  };
  return { options, ...renderHook(() => useAgentsDraft(options)) };
}

const names = (cards: { name: string }[]) => cards.map((c) => c.name);

describe("useAgentsDraft", () => {
  it("lists the catalog's specialists, then the rest being served", () => {
    const { result } = setup();

    // The served composed agent has its own section while editing.
    expect(names(result.current.cards)).toEqual([
      "docker_agent",
      "k8s_health_agent",
      "github_mcp_agent",
    ]);
    expect(names(result.current.live)).toEqual(["k8s_health_agent", "github_mcp_agent"]);
    expect(result.current.counts).toEqual({ tools: 3, confirm: 1, destructive: 0 });
    expect(result.current.excluded.map((s) => s.key)).toEqual(["es"]);
    expect(names(result.current.switchable)).toEqual(["docker_agent", "k8s_health_agent"]);
  });

  it("filters by show, and by a query over the agent or its tools", () => {
    const { result } = setup();

    act(() => result.current.setShow("off"));
    expect(names(result.current.visible)).toEqual(["docker_agent"]);

    act(() => result.current.setShow("on"));
    expect(names(result.current.visible)).toEqual(["k8s_health_agent", "github_mcp_agent"]);

    // A tool match keeps only the matching tools.
    act(() => result.current.setQuery("  SCALE "));
    expect(result.current.needle).toBe("scale");
    expect(result.current.visible).toHaveLength(1);
    expect(result.current.visible[0]?.tools.map((t) => t.name)).toEqual(["scale_deployment"]);

    // An agent match (here, its model) keeps every tool.
    act(() => result.current.setQuery("gemini"));
    expect(result.current.visible[0]?.tools).toHaveLength(2);

    act(() => result.current.setShow("off"));
    expect(result.current.visible).toEqual([]);
  });

  it("takes every name a composed agent may not reuse, but the one being edited", () => {
    const { result } = setup();

    const taken = result.current.takenNames(null);
    for (const name of [
      "docker_agent",
      "k8s_health_agent",
      "github_mcp_agent",
      "from_file",
      "orrery_chat_agent",
      "triager",
    ]) {
      expect(taken.has(name)).toBe(true);
    }
    expect(result.current.takenNames("triager").has("triager")).toBe(false);

    // Removed in the draft, its name is free again though the server serves it.
    act(() => result.current.remove(0));
    expect(result.current.takenNames(null).has("triager")).toBe(false);
  });

  it("is dirty after a switch, and clean again once it is switched back", () => {
    const { result, options } = setup();
    expect(result.current.dirty).toBe(false);
    expect(options.onDirtyChange).toHaveBeenLastCalledWith(false);

    act(() => result.current.toggle("docker"));
    expect(result.current.dirty).toBe(true);
    expect(result.current.changes).toBe("Turn on docker");
    expect(options.onDirtyChange).toHaveBeenLastCalledWith(true);
    expect(result.current.cards.find((c) => c.name === "docker_agent")).toMatchObject({
      on: true,
      savedOn: false,
    });

    act(() => result.current.toggle("docker"));
    expect(result.current.dirty).toBe(false);
    expect(options.onDirtyChange).toHaveBeenLastCalledWith(false);
  });

  it("counts an open agent dialog as unsaved, though the draft is clean", () => {
    const { result, options } = setup();

    act(() => result.current.setEditing({ index: 0, agent: triager }));
    expect(result.current.dirty).toBe(false);
    expect(options.onDirtyChange).toHaveBeenLastCalledWith(true);

    act(() => result.current.keep({ ...triager, description: "Sweeps every pod" }));
    expect(result.current.editing).toBeNull();
    expect(result.current.changes).toBe("Update triager");
  });

  it("discard goes back to the saved catalog", () => {
    const { result, options } = setup();

    act(() => result.current.setAll(true));
    act(() => result.current.remove(0));
    expect(result.current.changes).toBe("Turn on docker · remove triager");

    act(() => result.current.discard());
    expect(result.current.dirty).toBe(false);
    expect(result.current.composed).toEqual([triager]);
    expect(options.onDiscard).toHaveBeenCalledOnce();
  });

  it("saves the draft against the catalog's version", async () => {
    const { result, options } = setup();

    act(() => result.current.setAll(false));
    await act(() => result.current.save());

    expect(options.onSave).toHaveBeenCalledWith({
      version: 3,
      disabled_specialists: ["docker", "k8s"],
      agents: [triager],
    });
    expect(result.current.saving).toBe(false);
  });

  it("is read-only without editing enabled", () => {
    const { result } = setup({ catalog: { ...catalog, editing_enabled: false } });

    expect(result.current.editable).toBe(false);
    expect(result.current.switchable).toEqual([]);
    // Off the editor, a composed agent is just one more agent being served.
    expect(names(result.current.cards)).toContain("triager");
  });
});
