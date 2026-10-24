import { describe, expect, it } from "vitest";
import {
  ageLabel,
  describeChanges,
  draftChanges,
  guardCounts,
  nameProblem,
  toolsBySpecialist,
} from "./agentDraft";

const agent = (name: string, instruction = "x") => ({
  name,
  description: "d",
  instruction,
  tools: ["list_pods"],
});

describe("nameProblem", () => {
  it("follows the server's naming rule and refuses a name in use", () => {
    expect(nameProblem("", new Set())).toMatch(/give the agent a name/i);
    expect(nameProblem("Scaler", new Set())).toMatch(/lowercase/i);
    expect(nameProblem("a", new Set())).toMatch(/lowercase/i);
    expect(nameProblem("9lives", new Set())).toMatch(/lowercase/i);
    expect(nameProblem("k8s_health_agent", new Set(["k8s_health_agent"]))).toMatch(/exists/);
    expect(nameProblem("capacity_planner", new Set(["k8s_health_agent"]))).toBeNull();
  });
});

describe("draftChanges", () => {
  it("names what turns on, off, and which agents are added, updated or removed", () => {
    const changes = draftChanges(
      ["docker"],
      ["k8s"],
      [agent("keep"), agent("edit"), agent("gone")],
      [agent("keep"), agent("edit", "changed"), agent("fresh")],
    );
    expect(changes).toEqual({
      turnedOn: ["docker"],
      turnedOff: ["k8s"],
      added: ["fresh"],
      edited: ["edit"],
      removed: ["gone"],
    });
    expect(describeChanges(changes)).toBe(
      "Turn on docker · turn off k8s · add fresh · update edit · remove gone",
    );
  });

  it("says nothing when the draft matches what is saved", () => {
    expect(describeChanges(draftChanges(["a"], ["a"], [agent("x")], [agent("x")]))).toBeNull();
  });
});

describe("guardCounts and toolsBySpecialist", () => {
  const tools = [
    { ref: "list_pods", specialist: "k8s", guard: "read" as const, reason: "" },
    { ref: "scale", specialist: "k8s", guard: "confirm" as const, reason: "" },
    { ref: "delete_topic", specialist: "kafka", guard: "destructive" as const, reason: "" },
  ];

  it("counts the guards of the chosen tools", () => {
    expect(guardCounts(["list_pods", "scale", "delete_topic"], tools)).toEqual({
      read: 1,
      confirm: 1,
      destructive: 1,
    });
  });

  it("groups tools by specialist in catalog order", () => {
    expect(toolsBySpecialist(tools).map(([key, ts]) => [key, ts.length])).toEqual([
      ["k8s", 2],
      ["kafka", 1],
    ]);
  });
});

describe("ageLabel", () => {
  it("reads like the sidebar's history ages", () => {
    const now = 1_000_000_000_000;
    expect(ageLabel(now / 1000 - 10, now)).toBe("just now");
    expect(ageLabel(now / 1000 - 300, now)).toBe("5m ago");
    expect(ageLabel(now / 1000 - 3 * 3600, now)).toBe("3h ago");
    expect(ageLabel(now / 1000 - 2 * 86400, now)).toBe("2d ago");
  });
});
