import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { toneChip } from "../components/severity";
import { A2aInspector } from "./A2aInspector";
import type { RunView } from "./runState";
import type { A2aMessageEvent } from "./types";

function reply(status: string, task_state: string): A2aMessageEvent {
  return {
    type: "a2a.message",
    seq: 2,
    ts: 2,
    node_id: "writer",
    remote: "demo-writer",
    skill: "draft",
    direction: "inbound",
    method: "message/send",
    status,
    task_state,
    states: [task_state],
    task_id: "task-1234567890",
  } as A2aMessageEvent;
}

function renderReply(status: string, task_state: string) {
  const view = {
    a2a: [
      {
        key: "1",
        nodeId: "writer",
        remote: "demo-writer",
        skill: "draft",
        outbound: null,
        inbound: reply(status, task_state),
      },
    ],
  } as unknown as RunView;
  render(<A2aInspector registry={null} view={view} resolving={false} onResolve={() => {}} />);
  return screen.getByText(status);
}

describe("A2aInspector reply status", () => {
  it("does not paint a task still running as a failure", () => {
    const chip = renderReply("in_progress", "working");
    expect(chip.className).toContain(toneChip.warn);
    expect(chip.className).not.toContain(toneChip.crit);
  });

  it("keeps an error critical", () => {
    expect(renderReply("error", "failed").className).toContain(toneChip.crit);
  });
});
