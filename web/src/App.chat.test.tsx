import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import { storageKeys } from "./config";
import {
  makeToken,
  jsonResponse,
  sseFrame,
  progressStep,
  sseResponse,
  restoreGlobals,
} from "./test/appHarness";

// Chat turns: replies, streaming, approvals, triage and attachments.

afterEach(restoreGlobals);

describe("chat", () => {
  it("sends a message and renders the assistant reply", async () => {
    const fetchMock = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) =>
      jsonResponse({ session_id: "s1", response: "Kafka is healthy." }),
    );
    vi.stubGlobal("fetch", fetchMock);
    localStorage.setItem(storageKeys.token, makeToken({ sub: "bob", roles: ["admin"] }));

    const user = userEvent.setup();
    render(<App />);

    await user.type(screen.getByRole("textbox", { name: /message/i }), "kafka health?");
    await user.click(screen.getByRole("button", { name: /send/i }));

    expect(await screen.findByText("Kafka is healthy.")).toBeInTheDocument();

    // The server's session id threads the next turn onto the same conversation.
    await user.type(screen.getByRole("textbox", { name: /message/i }), "and the lag?");
    await user.click(screen.getByRole("button", { name: /send/i }));
    await waitFor(() => {
      const chatCalls = fetchMock.mock.calls.filter(([u]) => String(u).endsWith("/chat/stream"));
      expect(chatCalls).toHaveLength(2);
      expect(JSON.parse(String(chatCalls[1][1]?.body))).toMatchObject({ session_id: "s1" });
    });
    // Nothing about the conversation is written to disk.
    expect(localStorage.getItem("orrery.console.conversations")).toBeNull();
  });

  it("streams a turn: live trace of the specialists, then the answer once", async () => {
    const frame = (event: string, data: unknown) =>
      `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
    const step = (over: Record<string, unknown>) => ({
      call_id: "c",
      phase: "start",
      agent: "orrery_chat_agent",
      tool: "t",
      delegation: false,
      guarded: false,
      outcome: null,
      status: "",
      elapsed_ms: null,
      ...over,
    });
    const body = [
      frame("session", { session_id: "s7" }),
      frame("progress", step({ call_id: "d1", tool: "kafka_health_agent", delegation: true })),
      frame(
        "progress",
        step({ call_id: "t1", agent: "kafka_health_agent", tool: "get_consumer_lag" }),
      ),
      frame(
        "progress",
        step({
          call_id: "t1",
          phase: "end",
          agent: "kafka_health_agent",
          tool: "get_consumer_lag",
          outcome: "ok",
          elapsed_ms: 120,
        }),
      ),
      frame(
        "progress",
        step({
          call_id: "d1",
          phase: "end",
          tool: "kafka_health_agent",
          delegation: true,
          outcome: "ok",
          elapsed_ms: 900,
        }),
      ),
      frame("delta", { text: "Lag is ", author: "orrery_chat_agent" }),
      frame("delta", { text: "flat.", author: "orrery_chat_agent" }),
      frame("message", { session_id: "s7", text: "Lag is flat." }),
      frame("done", {}),
    ].join("");
    const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
      const u = String(url);
      if (u.endsWith("/chat/stream")) {
        return new Response(body, {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        });
      }
      if (u.includes("/activity")) return jsonResponse({ session_id: "s7", entries: [] });
      if (u.includes("/confirmations/pending")) return jsonResponse({ pending: null });
      if (u.includes("/triage"))
        return jsonResponse({ session_id: "s7", severity: null, report: null });
      return jsonResponse({ sessions: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
    localStorage.setItem(storageKeys.token, makeToken({ sub: "bob", roles: ["admin"] }));

    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByRole("textbox", { name: /message/i }), "kafka lag?");
    await user.click(screen.getByRole("button", { name: /send/i }));

    // The answer appears exactly once: the final message replaces the deltas
    // rather than being appended to them.
    expect(await screen.findByText("Lag is flat.")).toBeInTheDocument();
    expect(screen.getAllByText("Lag is flat.")).toHaveLength(1);
    // The finished trace folds into a summary naming who was consulted…
    const summary = await screen.findByText("Kafka · 1 tool call");
    // …and expands to the specialist's own tool call.
    await user.click(summary);
    expect(screen.getByText("get_consumer_lag")).toBeInTheDocument();
    // No polling while the turn ran: the side panes are read once, on `done`.
    const activityCalls = fetchMock.mock.calls.filter(([u]) => String(u).includes("/activity"));
    expect(activityCalls).toHaveLength(1);
  });

  it("tells a call awaiting approval apart from one a gate refused", async () => {
    const end = (call_id: string, tool: string, status: string) =>
      sseFrame(
        "progress",
        progressStep({ call_id, phase: "end", tool, guarded: true, outcome: "refused", status }),
      );
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: RequestInfo | URL) => {
        const u = String(url);
        if (u.endsWith("/chat/stream")) {
          return sseResponse([
            sseFrame("session", { session_id: "s8" }),
            end("a", "restart_container", "confirmation_required"),
            end("b", "delete_topic", "BLOCKED"),
            sseFrame("message", { session_id: "s8", text: "One needs you, one is off-limits." }),
            sseFrame("done", {}),
          ]);
        }
        if (u.includes("/confirmations/pending")) {
          return jsonResponse({
            pending: {
              tool_name: "restart_container",
              level: "confirm",
              args: { name: "orders-worker" },
              created_at: 1,
            },
          });
        }
        if (u.includes("/activity")) return jsonResponse({ session_id: "s8", entries: [] });
        if (u.includes("/triage"))
          return jsonResponse({ session_id: "s8", severity: null, report: null });
        return jsonResponse({ sessions: [] });
      }),
    );
    localStorage.setItem(storageKeys.token, makeToken({ sub: "bob", roles: ["operator"] }));

    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByRole("textbox", { name: /message/i }), "fix orders");
    await user.click(screen.getByRole("button", { name: /send/i }));

    const summary = await screen.findByText("2 tool calls · 1 awaiting approval · 1 refused");
    await user.click(summary);
    // The refusal says why, as policy rather than failure.
    expect(screen.getByText(/not allowed at this autonomy level/)).toBeInTheDocument();
    // The awaiting chip leads to the card that decides it.
    const panel = await screen.findByRole("alertdialog", { name: /pending confirmation/i });
    await user.click(screen.getByRole("button", { name: /restart_container.*review it/i }));
    expect(panel).toHaveFocus();
  });

  it("renders assistant markdown as formatted output, not raw asterisks", async () => {
    const reply = "Images:\n\n* **grafana/loki:3.7.3** (ID: `83dfa527a638`)\n* plain item";
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ session_id: "s1", response: reply })),
    );
    localStorage.setItem(storageKeys.token, makeToken({ sub: "bob", roles: ["admin"] }));

    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByRole("textbox", { name: /message/i }), "list images");
    await user.click(screen.getByRole("button", { name: /send/i }));

    const bold = await screen.findByText("grafana/loki:3.7.3");
    expect(bold.tagName).toBe("STRONG");
    expect(screen.getByText("83dfa527a638").tagName).toBe("CODE");
    // Scope to the rendered markdown list (the sidebar history is also a list).
    const markdownList = bold.closest("ul");
    expect(markdownList).not.toBeNull();
    expect(within(markdownList as HTMLElement).getAllByRole("listitem")).toHaveLength(2);
    expect(screen.queryByText(/\*\*grafana/)).not.toBeInTheDocument();
  });

  it("shows the tool-call table and confirmation panel after a guarded turn", async () => {
    const fetchMock = vi.fn(async (url: RequestInfo | URL, _init?: RequestInit) => {
      const u = String(url);
      if (u.includes("/activity")) {
        return jsonResponse({
          session_id: "s1",
          entries: [
            {
              operation: "restart_deployment",
              details: "[k8s] name=payment-api → confirmation_required",
              timestamp: "2026-07-19T00:00:00+00:00",
            },
          ],
        });
      }
      if (u.includes("/confirmations/pending")) {
        return jsonResponse({
          pending: {
            tool_name: "restart_deployment",
            level: "destructive",
            args: { name: "payment-api", namespace: "prod" },
            created_at: 1,
          },
        });
      }
      if (u.includes("/triage"))
        return jsonResponse({ session_id: "s1", severity: null, report: null });
      return jsonResponse({ session_id: "s1", response: "This action needs your approval." });
    });
    vi.stubGlobal("fetch", fetchMock);
    localStorage.setItem(storageKeys.token, makeToken({ sub: "bob", roles: ["admin"] }));

    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByRole("textbox", { name: /message/i }), "restart payment-api");
    await user.click(screen.getByRole("button", { name: /send/i }));

    // The confirmation panel is inline in the chat column.
    const panel = await screen.findByRole("alertdialog", { name: /pending confirmation/i });
    expect(panel).toHaveTextContent("restart_deployment");
    expect(panel).toHaveTextContent("payment-api");

    // Open the inspector's Tool calls tab and assert the table row landed.
    await user.click(screen.getByRole("button", { name: /tool calls/i }));
    const table = await screen.findByRole("table");
    expect(within(table).getByText("restart_deployment")).toBeInTheDocument();
    expect(within(table).getByText("confirmation_required")).toBeInTheDocument();

    // Approve sends the literal decision word through the normal chat flow —
    // the server-side requester-verified gate stays the authority.
    await user.click(screen.getByRole("button", { name: /approve/i }));
    await waitFor(() => {
      const chatCalls = fetchMock.mock.calls.filter(([u]) => String(u).endsWith("/chat/stream"));
      const last = chatCalls[chatCalls.length - 1];
      expect(JSON.parse(String(last[1]?.body))).toMatchObject({ message: "approve" });
    });
    // The transcript shows what was approved, not a bare "approve" bubble.
    const transcript = screen.getByRole("log", { name: /conversation transcript/i });
    expect(await within(transcript).findByText(/You approved/, { exact: false })).toHaveTextContent(
      /You approved restart_deployment \(payment-api\)/,
    );
    expect(within(transcript).queryByText("approve")).not.toBeInTheDocument();
  });

  it("shows the approval card only in the conversation it is waiting in", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: RequestInfo | URL) => {
        const path = new URL(String(url), "http://localhost").pathname;
        if (path === "/sessions") {
          return jsonResponse({
            sessions: [
              { session_id: "s2", title: "check kafka", last_update_time: 200 },
              { session_id: "s1", title: "fix orders", last_update_time: 100 },
            ],
          });
        }
        if (path === "/confirmations/pending") {
          return jsonResponse({
            pending: {
              tool_name: "restart_container",
              level: "confirm",
              args: { name: "orders-worker" },
              created_at: 1,
              session_id: "s1",
            },
          });
        }
        if (path.startsWith("/session/") && path.split("/").length === 3) {
          return jsonResponse({
            session_id: path.slice("/session/".length),
            title: "",
            messages: [],
            last_update_time: 0,
          });
        }
        return jsonResponse({ entries: [], severity: null, report: null });
      }),
    );
    localStorage.setItem(storageKeys.token, makeToken({ sub: "bob", roles: ["operator"] }));

    const user = userEvent.setup();
    render(<App />);

    // The newest conversation opens first: not where the action waits.
    const pointer = await screen.findByText(/waiting for your approval in/i);
    expect(pointer).toHaveTextContent("fix orders");
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Open fix orders" }));

    const panel = await screen.findByRole("alertdialog", { name: /pending confirmation/i });
    expect(panel).toHaveTextContent("restart_container");
    expect(screen.queryByText(/waiting for your approval in/i)).not.toBeInTheDocument();
  });

  it("runs a triage and auto-opens the verdict report", async () => {
    const fetchMock = vi.fn(async (url: RequestInfo | URL, _init?: RequestInit) => {
      const u = String(url);
      if (u.includes("/triage")) {
        return jsonResponse({
          session_id: "s1",
          severity: "degraded",
          report: "## Triage\n\nKafka: consumer lag growing on **orders**.",
        });
      }
      if (u.includes("/activity")) return jsonResponse({ session_id: "s1", entries: [] });
      if (u.includes("/confirmations/pending")) return jsonResponse({ pending: null });
      return jsonResponse({ session_id: "s1", response: "Triage complete: degraded." });
    });
    vi.stubGlobal("fetch", fetchMock);
    localStorage.setItem(storageKeys.token, makeToken({ sub: "bob", roles: ["admin"] }));

    const user = userEvent.setup();
    render(<App />);

    await user.click(screen.getByRole("button", { name: /run triage/i }));

    // The canned prompt went through the normal chat flow.
    await waitFor(() => {
      const chatCalls = fetchMock.mock.calls.filter(([u]) => String(u).endsWith("/chat/stream"));
      expect(chatCalls).toHaveLength(1);
      expect(JSON.parse(String(chatCalls[0][1]?.body)).message).toMatch(/incident triage/i);
    });

    // The inspector auto-opens on the triage tab; the report renders as prose.
    const orders = await screen.findByText("orders");
    expect(orders.tagName).toBe("STRONG");
    expect(screen.getAllByText("degraded").length).toBeGreaterThan(0);
  });

  it("attaches a file, sends it as multipart, and shows it as a chip", async () => {
    const fetchMock = vi.fn(async (url: RequestInfo | URL, _init?: RequestInit) => {
      const u = String(url);
      if (u.endsWith("/me")) {
        return jsonResponse({
          subject: "bob",
          role: "operator",
          autonomy_level: null,
          model_provider: "anthropic",
          model_name: "claude-haiku-5-5",
          self_test_available: false,
          attachments: {
            max_files: 4,
            max_file_bytes: 1024,
            max_total_bytes: 4096,
            images: true,
            image_types: ["image/png", "image/jpeg", "image/gif", "image/webp"],
          },
        });
      }
      if (u.includes("/chat"))
        return jsonResponse({ session_id: "s1", response: "It ran out of memory." });
      return jsonResponse({
        sessions: [],
        entries: [],
        pending: null,
        severity: null,
        report: null,
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    localStorage.setItem(storageKeys.token, makeToken({ sub: "bob", roles: ["operator"] }));
    const user = userEvent.setup();
    const { container } = render(<App />);

    await screen.findByRole("button", { name: "Attach files" });
    const picker = container.querySelector<HTMLInputElement>('input[type="file"]');
    expect(picker).not.toBeNull();
    await user.upload(picker as HTMLInputElement, [
      new File(["OOMKilled"], "app.log", { type: "text/plain" }),
      new File(["x".repeat(2048)], "huge.log", { type: "text/plain" }),
    ]);

    // The oversized one is refused before upload; the other waits as a chip.
    expect(await screen.findByRole("alert")).toHaveTextContent("huge.log is over the 1 KB limit.");
    const queued = screen.getByRole("list", { name: "Attachments" });
    expect(within(queued).getByText("app.log")).toBeInTheDocument();

    // A file alone is a valid message.
    await user.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([u]) => String(u).includes("/chat"));
      expect(call?.[1]?.body).toBeInstanceOf(FormData);
    });
    const call = fetchMock.mock.calls.find(([u]) => String(u).includes("/chat"));
    const form = call?.[1]?.body as FormData;
    expect((form.getAll("files") as File[]).map((f) => f.name)).toEqual(["app.log"]);
    const sent = await screen.findByRole("list", { name: "Attached files" });
    expect(within(sent).getByText("app.log")).toBeInTheDocument();
    expect(await screen.findByText("It ran out of memory.")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Attachments" })).not.toBeInTheDocument();
  });

  it("hides attaching when the server doesn't offer it", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({ sessions: [], entries: [], pending: null, severity: null, report: null }),
      ),
    );
    localStorage.setItem(storageKeys.token, makeToken({ sub: "bob", roles: ["operator"] }));
    render(<App />);

    await screen.findByRole("textbox", { name: /message/i });
    expect(screen.queryByRole("button", { name: "Attach files" })).not.toBeInTheDocument();
  });
});
