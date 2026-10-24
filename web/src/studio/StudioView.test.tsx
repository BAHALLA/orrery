import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { stubReactFlowDom, stubWideScreen } from "../test/reactflow";
import { releaseNotesRun } from "./fixtures/events";
import { DEMO_REGISTRY } from "./fixtures/registry";
import schema from "./fixtures/studio-schema.json";
import StudioView from "./StudioView";
import type { StudioEvent } from "./types";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const frame = (e: StudioEvent) => `id: ${e.seq}\nevent: ${e.type}\ndata: ${JSON.stringify(e)}\n\n`;

/** An SSE body the test feeds one event at a time, then closes. */
function controlledStream() {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  const encoder = new TextEncoder();
  return {
    response: new Response(body, { headers: { "Content-Type": "text/event-stream" } }),
    push: (...events: StudioEvent[]) =>
      events.forEach((e) => controller.enqueue(encoder.encode(frame(e)))),
    close: () => controller.close(),
  };
}

interface Routes {
  start?: () => Response;
  events?: (init: RequestInit, call: number) => Response;
  schema?: () => Response;
}

function stubStudio(routes: Routes = {}) {
  const calls: { url: string; init: RequestInit }[] = [];
  let eventCalls = 0;
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input);
    calls.push({ url, init });
    if (url.endsWith("/studio/schema")) return routes.schema?.() ?? json(schema);
    if (url.startsWith("/studio/registry")) return json(DEMO_REGISTRY);
    if (url.endsWith("/studio/runs") && init.method === "POST") {
      return routes.start?.() ?? json({ run_id: "run-1" }, 202);
    }
    if (url.endsWith("/events")) {
      eventCalls += 1;
      return (
        routes.events?.(init, eventCalls) ??
        new Response("", { headers: { "Content-Type": "text/event-stream" } })
      );
    }
    if (url.endsWith("/cancel")) return json({ run_id: "run-1", status: "running" });
    return json({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

const node = (name: RegExp) => screen.getByRole("group", { name });

async function loadDemo(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Research → writer → critic" }));
  expect(await screen.findByRole("group", { name: /^LLM agent writer/ })).toBeInTheDocument();
}

beforeEach(() => {
  stubReactFlowDom();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Studio canvas", () => {
  it("builds a pipeline from a template and checks it live", async () => {
    stubStudio();
    const user = userEvent.setup();
    render(<StudioView token="t" />);

    // The patterns P0a cannot run are listed, but not loadable.
    await user.click(await screen.findByRole("button", { name: "Patterns" }));
    const dialog = screen.getByRole("dialog", { name: "Start from a pattern" });
    const router = within(dialog).getByRole("button", { name: /^Router/ });
    expect(router).toHaveAttribute("aria-disabled", "true");
    expect(router).toHaveTextContent("needs P0b");
    await user.click(router);
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: /^Pipeline/ }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(node(/^LLM agent analyst$/)).toBeInTheDocument();
    expect(node(/^LLM agent summariser$/)).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Valid" })).toBeInTheDocument();

    // Emptying an instruction is caught as it happens, on the node and in the field.
    // Selected from the keyboard, as React Flow allows (a pointer click works too).
    node(/^LLM agent summariser/).focus();
    await user.keyboard("{Enter}");
    const instruction = await screen.findByLabelText("Instruction");
    await user.clear(instruction);
    expect(await screen.findByText("is required")).toBeInTheDocument();
    expect(node(/^LLM agent summariser, 1 problem$/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "1 problem" })).toBeInTheDocument();
  });

  it("adds nodes from the palette with the keyboard and wires them in", async () => {
    stubWideScreen();
    stubStudio();
    const user = userEvent.setup();
    render(<StudioView token="t" />);

    const palette = await screen.findByRole("complementary", { name: "Palette" });
    const router = within(palette).getByRole("button", { name: /^Router/ });
    expect(router).toHaveAttribute("aria-disabled", "true");
    expect(router).toHaveTextContent("coming in P0b");

    within(palette)
      .getByRole("button", { name: /^LLM agent/ })
      .focus();
    await user.keyboard("{Enter}");
    await user.keyboard("{Enter}");
    expect(node(/^LLM agent agent,/)).toBeInTheDocument();
    expect(node(/^LLM agent agent_2,/)).toBeInTheDocument();
    // The second was appended after the first, as a pipeline step.
    expect(screen.getByRole("group", { name: "Edge from agent to agent_2" })).toBeInTheDocument();
    // agent_2 is selected: its inspector is open.
    expect(screen.getByRole("heading", { name: "agent_2" })).toBeInTheDocument();
  });
});

describe("Studio runs", () => {
  it("runs the demo against a live stream and animates the graph", async () => {
    const stream = controlledStream();
    const calls = stubStudio({ events: () => stream.response });
    const user = userEvent.setup();
    render(<StudioView token="t" />);
    await loadDemo(user);

    await user.click(screen.getByRole("button", { name: /^run$/i }));
    const runInput = await screen.findByLabelText("Run input");
    expect(runInput).toHaveFocus();
    await user.type(runInput, "Kafka consumer lag");
    await user.click(within(screen.getByRole("tabpanel")).getByRole("button", { name: /^run$/i }));

    const post = calls.find((c) => c.url.endsWith("/studio/runs"));
    expect(JSON.parse(String(post?.init.body))).toMatchObject({
      input: "Kafka consumer lag",
      spec: { name: "release-notes" },
    });

    const events = releaseNotesRun();
    stream.push(...events.slice(0, 6));
    expect(
      await screen.findByRole("group", { name: "LLM agent writer, active" }),
    ).toBeInTheDocument();
    expect(node(/^LLM agent research, ok$/)).toBeInTheDocument();
    expect(node(/^LLM agent critic$/)).toBeInTheDocument();
    // The canvas is locked while the run is live.
    expect(screen.getByText("locked while running")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /cancel run/i })).toBeInTheDocument();

    stream.push(...events.slice(6));
    stream.close();
    expect(await screen.findByText("2,500")).toBeInTheDocument();
    expect(node(/^LLM agent critic, ok$/)).toBeInTheDocument();
    const panel = screen.getByRole("tabpanel");
    expect(within(panel).getByText("succeeded")).toBeInTheDocument();
    expect(
      within(panel).getByText(/research called tirraflow.search_articles · success/),
    ).toBeInTheDocument();

    // The A2A tab tells the call as the protocol does: request, task states, reply.
    await user.click(screen.getByRole("tab", { name: /A2A/ }));
    const call = screen.getByRole("list", { name: "Call from writer to demo-writer" });
    expect(within(call).getByText("message/send")).toBeInTheDocument();
    const states = within(call).getByRole("list", { name: "Task states" });
    expect(
      within(states)
        .getAllByRole("listitem")
        .map((li) => li.textContent?.replace("→", "")),
    ).toEqual(["submitted", "working", "completed"]);

    await user.click(screen.getByRole("tab", { name: /MCP/ }));
    expect(
      within(screen.getByRole("tabpanel")).getByText("tirraflow.search_articles"),
    ).toBeInTheDocument();
  });

  it("resumes a dropped stream from the last event it saw", async () => {
    const events = releaseNotesRun();
    const seen: (string | undefined)[] = [];
    stubStudio({
      events: (init, call) => {
        seen.push((init.headers as Record<string, string>)["Last-Event-ID"]);
        const part = call === 1 ? events.slice(0, 3) : events.slice(2);
        return new Response(part.map(frame).join(""), {
          headers: { "Content-Type": "text/event-stream" },
        });
      },
    });
    const user = userEvent.setup();
    render(<StudioView token="t" />);
    await loadDemo(user);
    await user.click(screen.getByRole("tab", { name: /^run/i }));
    await user.type(screen.getByLabelText("Run input"), "lag");
    await user.click(within(screen.getByRole("tabpanel")).getByRole("button", { name: /^run$/i }));

    expect(await screen.findByText("2,500", undefined, { timeout: 3000 })).toBeInTheDocument();
    expect(seen).toEqual([undefined, "3"]);
    // The replayed event was not counted twice.
    expect(within(screen.getByRole("tabpanel")).getByText("11 events")).toBeInTheDocument();
  });

  it("shows a budget hit plainly", async () => {
    const [start, enter] = releaseNotesRun();
    stubStudio({
      events: () =>
        new Response(
          [
            start!,
            enter!,
            {
              seq: 3,
              run_id: "run-1",
              ts: 101,
              type: "budget.hit",
              budget: "max_tool_calls",
              limit: 12,
              used: 12,
            },
            {
              seq: 4,
              run_id: "run-1",
              ts: 101.1,
              type: "run.end",
              outcome: "budget_exhausted",
              error: "budget max_tool_calls",
              totals: {
                tokens: { input: 1, output: 1, total: 2 },
                tokens_by_model: {},
                tool_calls: 12,
                delegations: 0,
                remote_calls: 0,
                wall_ms: 1100,
              },
            },
          ]
            .map((e) => frame(e as StudioEvent))
            .join(""),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    });
    const user = userEvent.setup();
    render(<StudioView token="t" />);
    await loadDemo(user);
    await user.click(screen.getByRole("tab", { name: /^run/i }));
    await user.type(screen.getByLabelText("Run input"), "lag");
    await user.click(within(screen.getByRole("tabpanel")).getByRole("button", { name: /^run$/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Budget hit: Tool calls");
    expect(alert).toHaveTextContent("12 used of 12");
    expect(node(/^LLM agent research, budget exhausted$/)).toBeInTheDocument();
  });

  it("maps the server's 422 onto the node and field it names", async () => {
    stubStudio({
      start: () =>
        json(
          {
            detail: {
              errors: [
                {
                  loc: ["spec", "nodes", 1, "tools", 0],
                  msg: "remote agent 'demo-writer' is not registered for skill 'draft'",
                  code: "unknown_tool",
                },
                {
                  loc: ["spec", "budgets", "max_tokens"],
                  msg: "60000 exceeds this deployment's ceiling of 50000",
                  code: "over_ceiling",
                },
              ],
            },
          },
          422,
        ),
    });
    const user = userEvent.setup();
    render(<StudioView token="t" />);
    await loadDemo(user);
    await user.click(screen.getByRole("tab", { name: /^run/i }));
    await user.type(screen.getByLabelText("Run input"), "lag");
    await user.click(within(screen.getByRole("tabpanel")).getByRole("button", { name: /^run$/i }));

    expect(
      await screen.findByRole("group", { name: "LLM agent writer, 1 problem" }),
    ).toBeInTheDocument();
    // The inspector opens on that node, the error under its tool.
    expect(screen.getByRole("heading", { name: "writer" })).toBeInTheDocument();
    const selected = screen.getByRole("list", { name: "Selected tools" });
    expect(within(selected).getByText(/not registered for skill 'draft'/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "2 problems" })).toBeInTheDocument();

    // Editing the node retires the server's verdict on it; the budget one stays.
    await user.click(
      within(selected).getByRole("button", { name: "Remove a2a:demo-writer.draft" }),
    );
    expect(node(/^LLM agent writer$/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "1 problem" })).toBeInTheDocument();
  });

  it("explains a deployment without the Studio", async () => {
    stubStudio({ schema: () => json({ detail: "Not Found" }, 404) });
    render(<StudioView token="t" />);
    expect(
      await screen.findByRole("heading", { name: "The Studio is not enabled here" }),
    ).toBeInTheDocument();
  });
});

describe("Studio import and export", () => {
  it("loads a pasted spec, expanding the remote-agent shorthand, and exports it back", async () => {
    stubStudio();
    const user = userEvent.setup();
    render(<StudioView token="t" />);
    await user.click(await screen.findByRole("button", { name: /import/i }));
    const spec = {
      spec_version: 1,
      name: "hand-off",
      nodes: [
        { id: "notes", kind: "llm_agent", instruction: "Take notes." },
        { id: "writer", kind: "remote_a2a", remote: "demo-writer.draft" },
      ],
      edges: [{ from: "notes", to: "writer" }],
    };
    await user.click(screen.getByLabelText(/paste a spec/i));
    await user.paste(JSON.stringify(spec));
    await user.click(screen.getByRole("button", { name: "Load onto the canvas" }));

    expect(await screen.findByRole("group", { name: /^LLM agent writer/ })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /export/i }));
    const dialog = screen.getByRole("dialog", { name: "Composition JSON" });
    const exported = JSON.parse(
      within(dialog).getByRole("tabpanel").querySelector("pre")!.textContent!,
    );
    expect(exported.nodes[1]).toMatchObject({
      kind: "llm_agent",
      tools: ["a2a:demo-writer.draft"],
    });
    expect(
      within(dialog).getByRole("button", { name: /download hand-off.spec.json/i }),
    ).toBeInTheDocument();
  });

  it("refuses what it cannot read, and says why", async () => {
    stubStudio();
    const user = userEvent.setup();
    render(<StudioView token="t" />);
    await user.click(await screen.findByRole("button", { name: /import/i }));
    await user.click(screen.getByLabelText(/paste a spec/i));
    await user.paste('{"nodes": "no"}');
    await user.click(screen.getByRole("button", { name: "Load onto the canvas" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("no `nodes` list");
    await waitFor(() => expect(screen.getByRole("dialog")).toBeInTheDocument());
  });
});
