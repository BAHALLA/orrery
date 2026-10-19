import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import { storageKeys } from "./config";

/** A minimal unsigned JWT for the given claims (display decoding only). */
function makeToken(payload: Record<string, unknown>): string {
  const b64 = (obj: unknown) =>
    btoa(JSON.stringify(obj)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${b64({ alg: "HS256", typ: "JWT" })}.${b64(payload)}.sig`;
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("App auth flow", () => {
  it("shows the token gate when unauthenticated", () => {
    render(<App />);
    expect(screen.getByRole("heading", { name: /orrery console/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/jwt bearer token/i)).toBeInTheDocument();
  });

  it("enters the console and shows the identity badge after connecting", async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.type(
      screen.getByLabelText(/jwt bearer token/i),
      makeToken({ sub: "alice", roles: ["operator"] }),
    );
    await user.click(screen.getByRole("button", { name: /connect/i }));

    expect(await screen.findByText("alice")).toBeInTheDocument();
    expect(screen.getByText("operator")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: /message/i })).toBeInTheDocument();
  });

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
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: RequestInfo | URL) => {
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
      }),
    );
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
  });

  it("restores history from the server in a browser that has never seen it", async () => {
    // The point of the server-backed history: a different machine (or cleared
    // site data) rebuilds the sidebar and the transcript from the session store.
    const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
      const path = new URL(String(url), "http://localhost").pathname;
      if (path === "/sessions") {
        return jsonResponse({
          sessions: [
            { session_id: "s-old", title: "check kafka", last_update_time: 100 },
            { session_id: "s-new", title: "restart payment-api", last_update_time: 200 },
          ],
        });
      }
      if (path === "/session/s-new") {
        return jsonResponse({
          session_id: "s-new",
          title: "restart payment-api",
          messages: [
            { role: "user", text: "restart payment-api", at: 200 },
            { role: "assistant", text: "Rolled the deployment.", at: 201 },
          ],
          last_update_time: 201,
        });
      }
      return jsonResponse({ entries: [], pending: null, severity: null, report: null });
    });
    vi.stubGlobal("fetch", fetchMock);
    localStorage.setItem(storageKeys.token, makeToken({ sub: "bob", roles: ["admin"] }));

    render(<App />);

    // Both conversations are listed, newest first, with the latest one open.
    const history = await screen.findByRole("navigation", { name: /conversation history/i });
    await waitFor(() =>
      expect(within(history).getByText("restart payment-api")).toBeInTheDocument(),
    );
    expect(within(history).getByText("check kafka")).toBeInTheDocument();
    expect(await screen.findByText("Rolled the deployment.")).toBeInTheDocument();
  });

  it("opens an older conversation and fetches its transcript on demand", async () => {
    const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
      const path = new URL(String(url), "http://localhost").pathname;
      if (path === "/sessions") {
        return jsonResponse({
          sessions: [
            { session_id: "s-old", title: "check kafka", last_update_time: 100 },
            { session_id: "s-new", title: "restart payment-api", last_update_time: 200 },
          ],
        });
      }
      if (path === "/session/s-old") {
        return jsonResponse({
          session_id: "s-old",
          title: "check kafka",
          messages: [{ role: "assistant", text: "All brokers up.", at: 100 }],
          last_update_time: 100,
        });
      }
      if (path === "/session/s-new") {
        return jsonResponse({
          session_id: "s-new",
          title: "restart payment-api",
          messages: [],
          last_update_time: 200,
        });
      }
      return jsonResponse({ entries: [], pending: null, severity: null, report: null });
    });
    vi.stubGlobal("fetch", fetchMock);
    localStorage.setItem(storageKeys.token, makeToken({ sub: "bob", roles: ["admin"] }));

    const user = userEvent.setup();
    render(<App />);

    const history = await screen.findByRole("navigation", { name: /conversation history/i });
    await waitFor(() => expect(within(history).getByText("check kafka")).toBeInTheDocument());
    // The older conversation's transcript is only fetched when it is opened.
    expect(fetchMock.mock.calls.some(([u]) => String(u).endsWith("/session/s-old"))).toBe(false);

    await user.click(within(history).getByText("check kafka"));

    expect(await screen.findByText("All brokers up.")).toBeInTheDocument();
  });

  it("deletes a conversation from the store, not just the sidebar", async () => {
    const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(url), "http://localhost").pathname;
      if (init?.method === "DELETE") return new Response(null, { status: 204 });
      if (path === "/sessions") {
        return jsonResponse({
          sessions: [
            { session_id: "s1", title: "check kafka", last_update_time: 100 },
            { session_id: "s2", title: "restart payment-api", last_update_time: 200 },
          ],
        });
      }
      if (path.startsWith("/session/")) {
        return jsonResponse({
          session_id: path.slice("/session/".length),
          title: "",
          messages: [],
          last_update_time: 0,
        });
      }
      return jsonResponse({ entries: [], pending: null, severity: null, report: null });
    });
    vi.stubGlobal("fetch", fetchMock);
    localStorage.setItem(storageKeys.token, makeToken({ sub: "bob", roles: ["admin"] }));

    const user = userEvent.setup();
    render(<App />);

    const history = await screen.findByRole("navigation", { name: /conversation history/i });
    await waitFor(() => expect(within(history).getByText("check kafka")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /delete conversation: check kafka/i }));

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([u, init]) => String(u).endsWith("/session/s1") && init?.method === "DELETE",
        ),
      ).toBe(true),
    );
    await waitFor(() => expect(within(history).queryByText("check kafka")).not.toBeInTheDocument());
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

  it("keeps conversations in a sidebar history", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ session_id: "s1", response: "ok" })),
    );
    localStorage.setItem(storageKeys.token, makeToken({ sub: "bob", roles: ["admin"] }));

    const user = userEvent.setup();
    render(<App />);

    await user.type(screen.getByRole("textbox", { name: /message/i }), "check kafka");
    await user.click(screen.getByRole("button", { name: /send/i }));
    await screen.findByText("ok");

    // The conversation is titled from the first message and listed under History.
    const history = screen.getByRole("navigation", { name: /conversation history/i });
    expect(within(history).getByText("check kafka")).toBeInTheDocument();

    // New chat adds a fresh entry and clears the transcript.
    await user.click(screen.getByRole("button", { name: /new chat/i }));
    expect(screen.queryByText("ok")).not.toBeInTheDocument();
  });

  it("keeps one session per thread: New chat starts another, going back continues the first", async () => {
    // The server mints a session per id-less turn and echoes a known id back.
    let minted = 0;
    const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url).endsWith("/chat/stream")) {
        const body = JSON.parse(String(init?.body));
        const session_id = body.session_id || `s${++minted}`;
        return jsonResponse({ session_id, response: `reply in ${session_id}` });
      }
      return jsonResponse({ entries: [], pending: null, severity: null, report: null });
    });
    vi.stubGlobal("fetch", fetchMock);
    localStorage.setItem(storageKeys.token, makeToken({ sub: "bob", roles: ["admin"] }));
    const sent = () =>
      fetchMock.mock.calls
        .filter(([u]) => String(u).endsWith("/chat/stream"))
        .map(([, init]) => JSON.parse(String(init?.body)).session_id ?? null);

    const user = userEvent.setup();
    render(<App />);
    const say = async (text: string) => {
      await user.type(screen.getByRole("textbox", { name: /message/i }), text);
      await user.click(screen.getByRole("button", { name: /send/i }));
    };

    await say("check kafka");
    await screen.findByText("reply in s1");
    await say("and the lag?");
    await waitFor(() => expect(sent()).toEqual([null, "s1"]));

    await user.click(screen.getByRole("button", { name: /new chat/i }));
    await say("restart payment-api");
    await screen.findByText("reply in s2");
    await waitFor(() => expect(sent()).toEqual([null, "s1", null]));

    const history = screen.getByRole("navigation", { name: /conversation history/i });
    await user.click(within(history).getByText("check kafka"));
    await say("still lagging?");
    await waitFor(() => expect(sent()).toEqual([null, "s1", null, "s1"]));
  });
});

describe("resilience and safety posture", () => {
  const token = makeToken({ sub: "alice", roles: ["operator"] });

  /** Route each endpoint to a canned response; /chat is caller-controlled. */
  function stubApi(chat: () => Promise<Response>, me?: Record<string, unknown>) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/chat")) return chat();
        if (url.endsWith("/me")) {
          return jsonResponse(
            me ?? {
              subject: "alice",
              role: "operator",
              autonomy_level: "L3",
              model_provider: "gemini",
              model_name: "gemini-3-pro",
              self_test_available: true,
            },
          );
        }
        if (url.endsWith("/admin/agents")) {
          return jsonResponse({
            agents: [
              {
                name: "k8s_health_agent",
                description: "Kubernetes",
                source: "builtin",
                tools: [
                  { name: "list_pods", guard: "read", reason: "" },
                  { name: "delete_pod", guard: "destructive", reason: "deletes a pod" },
                ],
              },
              {
                name: "github_mcp_agent",
                description: "GitHub",
                source: "mcp",
                tools: [{ name: "github_*", guard: "dynamic", reason: "" }],
              },
            ],
          });
        }
        if (url.includes("/onboarding/selftest")) {
          return jsonResponse({
            ok: false,
            checks: [
              {
                name: "model",
                label: "gemini / gemini-3-pro",
                ok: true,
                detail: "Reached gemini.",
                hint: "",
                duration_ms: 120,
              },
              {
                name: "kafka",
                label: "Kafka",
                ok: false,
                detail: "Connection refused to broker-1:9092",
                hint: "Set KAFKA_BOOTSTRAP_SERVERS to a reachable broker list.",
                duration_ms: 40,
              },
            ],
          });
        }
        return jsonResponse({ entries: [], pending: null, severity: null, report: null });
      }),
    );
  }

  it("shows the server-resolved autonomy level next to the role", async () => {
    stubApi(async () => jsonResponse({ session_id: "s1", response: "hi" }));
    localStorage.setItem(storageKeys.token, token);
    render(<App />);

    // The token says nothing about autonomy — only the server knows it.
    expect(await screen.findByTitle(/autonomy l3/i)).toHaveTextContent("L3");
  });

  it("shows admins the agent inventory with each tool's guard level", async () => {
    stubApi(async () => jsonResponse({ session_id: "s1", response: "hi" }), {
      subject: "alice",
      role: "admin",
      autonomy_level: null,
      model_provider: "gemini",
      model_name: "",
      self_test_available: true,
    });
    localStorage.setItem(storageKeys.token, makeToken({ sub: "alice", roles: ["admin"] }));
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: /^agents$/i }));

    const list = await screen.findByRole("list", { name: "Agents" });
    expect(within(list).getByText("k8s_health_agent")).toBeInTheDocument();
    expect(within(list).getByText("MCP")).toBeInTheDocument();
    expect(within(list).getByText("delete_pod")).toHaveAttribute(
      "title",
      expect.stringMatching(/destructive.*deletes a pod/i),
    );
  });

  it("lets an admin switch a specialist off and compose an agent, then saves both", async () => {
    let version = 0;
    const puts: unknown[] = [];
    const catalogBody = () => ({
      editing_enabled: true,
      applied_version: version,
      catalog: { version, disabled_specialists: [], agents: [], updated_by: "", updated_at: 0 },
      specialists: [
        {
          key: "docker",
          agent_name: "docker_agent",
          description: "",
          source: "builtin",
          available: true,
        },
        {
          key: "k8s",
          agent_name: "k8s_health_agent",
          description: "",
          source: "builtin",
          available: true,
        },
      ],
      tools: [
        { ref: "list_pods", specialist: "k8s", guard: "read", reason: "" },
        { ref: "scale_deployment", specialist: "k8s", guard: "confirm", reason: "scales" },
      ],
      file_agents: [],
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/admin/catalog") && init?.method === "PUT") {
          puts.push(JSON.parse(String(init.body)));
          if (puts.length > 1) {
            return new Response(
              JSON.stringify({
                detail:
                  "The catalog changed since you loaded it (now version 2). Reload and reapply your edit.",
              }),
              { status: 409 },
            );
          }
          version = 1;
          return jsonResponse(catalogBody());
        }
        if (url.endsWith("/admin/catalog")) return jsonResponse(catalogBody());
        if (url.endsWith("/admin/agents")) return jsonResponse({ agents: [] });
        if (url.endsWith("/me")) {
          return jsonResponse({
            subject: "alice",
            role: "admin",
            autonomy_level: null,
            model_provider: "gemini",
            model_name: "",
            self_test_available: true,
          });
        }
        return jsonResponse({
          sessions: [],
          entries: [],
          pending: null,
          severity: null,
          report: null,
        });
      }),
    );
    localStorage.setItem(storageKeys.token, makeToken({ sub: "alice", roles: ["admin"] }));
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: /^agents$/i }));
    await user.click(await screen.findByRole("switch", { name: "Enable docker" }));

    await user.click(screen.getByRole("button", { name: "New agent" }));
    const form = screen.getByRole("form", { name: "Agent editor" });
    await user.type(within(form).getByLabelText("Name"), "scaler");
    await user.type(within(form).getByLabelText(/^Description/), "Scales workloads on request");
    await user.type(within(form).getByLabelText("Instruction"), "Check, then scale.");
    await user.click(within(form).getByRole("checkbox", { name: /scale_deployment/ }));
    await user.click(within(form).getByRole("button", { name: "Keep agent" }));
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(await screen.findByText(/saved as version 1/i)).toBeInTheDocument();
    expect(puts[0]).toEqual({
      version: 0,
      disabled_specialists: ["docker"],
      agents: [
        {
          name: "scaler",
          description: "Scales workloads on request",
          instruction: "Check, then scale.",
          tools: ["scale_deployment"],
        },
      ],
    });

    // A concurrent edit elsewhere: the save is refused and the reason shown.
    await user.click(screen.getByRole("switch", { name: "Enable k8s" }));
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/changed since you loaded it/i);
  });

  it("keeps an unsaved agents draft across the chat and a triage verdict", async () => {
    let release: () => void = () => {};
    const replyGate = new Promise<void>((resolve) => {
      release = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/admin/catalog")) {
          return jsonResponse({
            editing_enabled: true,
            applied_version: 0,
            catalog: {
              version: 0,
              disabled_specialists: ["docker", "k8s"],
              agents: [],
              updated_by: "",
              updated_at: 0,
            },
            specialists: ["docker", "k8s"].map((key) => ({
              key,
              agent_name: `${key}_agent`,
              description: "",
              source: "builtin",
              available: true,
            })),
            tools: [],
            file_agents: [],
          });
        }
        if (url.endsWith("/admin/agents")) {
          return jsonResponse({
            agents: [
              {
                name: "pager_agent",
                description: "Handles paging rotations",
                source: "plugin",
                tools: [],
              },
            ],
          });
        }
        if (url.endsWith("/me")) {
          return jsonResponse({
            subject: "alice",
            role: "admin",
            autonomy_level: null,
            model_provider: "gemini",
            model_name: "",
            self_test_available: true,
          });
        }
        if (url.includes("/triage")) {
          return jsonResponse({ session_id: "s1", severity: "degraded", report: "Kafka lag." });
        }
        if (url.includes("/chat")) {
          await replyGate;
          return jsonResponse({ session_id: "s1", response: "Triage complete: degraded." });
        }
        return jsonResponse({
          sessions: [],
          entries: [],
          pending: null,
          severity: null,
          report: null,
        });
      }),
    );
    localStorage.setItem(storageKeys.token, makeToken({ sub: "alice", roles: ["admin"] }));
    const user = userEvent.setup();
    render(<App />);

    // A sweep is running when the admin goes to edit agents.
    await user.click(await screen.findByRole("button", { name: /run triage/i }));
    await user.click(await screen.findByRole("button", { name: /^agents$/i }));
    await user.click(await screen.findByRole("switch", { name: "Enable docker" }));
    expect(screen.getByRole("button", { name: /^agents unsaved$/i })).toBeInTheDocument();

    // The verdict lands mid-edit: badged here, not a forced switch to the chat.
    release();
    const verdict = await screen.findByRole("button", { name: /triage verdict ready/i });
    expect(screen.getByRole("switch", { name: "Enable docker" })).toHaveAttribute(
      "aria-checked",
      "true",
    );

    // Away to the chat and back: the draft is still there.
    await user.click(verdict);
    expect(screen.queryByRole("button", { name: /triage verdict ready/i })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /^agents unsaved$/i }));
    const docker = screen.getByRole("switch", { name: "Enable docker" });
    expect(docker).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeInTheDocument();

    // Switching it back leaves ["k8s", "docker"]: the same set, so nothing to save.
    await user.click(docker);
    expect(screen.queryByRole("button", { name: "Save changes" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^agents$/i })).toBeInTheDocument();

    // A tool-less agent found by its description alone stays in the results.
    await user.type(screen.getByRole("searchbox", { name: /find an agent/i }), "paging");
    expect(
      within(screen.getByRole("list", { name: "Agents" })).getByText("pager_agent"),
    ).toBeInTheDocument();

    // A half-filled agent dialog is unsaved work as well.
    await user.click(screen.getByRole("button", { name: "New agent" }));
    expect(screen.getByRole("button", { name: /^agents unsaved$/i })).toBeInTheDocument();
  });

  it("hides the agent inventory from non-admins", async () => {
    stubApi(async () => jsonResponse({ session_id: "s1", response: "hi" }));
    localStorage.setItem(storageKeys.token, token);
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: /check my environment/i }));
    await screen.findByRole("tab", { name: /system/i });

    expect(screen.queryByRole("button", { name: /^agents$/i })).not.toBeInTheDocument();
  });

  it("runs the environment check and names what to fix", async () => {
    stubApi(async () => jsonResponse({ session_id: "s1", response: "hi" }));
    localStorage.setItem(storageKeys.token, token);
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: /check my environment/i }));
    await user.click(await screen.findByRole("button", { name: /^run checks$/i }));

    expect(await screen.findByText(/connection refused to broker-1:9092/i)).toBeInTheDocument();
    expect(
      screen.getByText(/set kafka_bootstrap_servers to a reachable broker list/i),
    ).toBeInTheDocument();
  });

  it("offers a retry that does not duplicate the user's message", async () => {
    let attempt = 0;
    stubApi(async () => {
      attempt += 1;
      if (attempt === 1) return new Response("boom", { status: 503 });
      return jsonResponse({ session_id: "s1", response: "recovered" });
    });
    localStorage.setItem(storageKeys.token, token);
    const user = userEvent.setup();
    render(<App />);

    await user.type(screen.getByRole("textbox", { name: /message/i }), "kafka health?");
    await user.click(screen.getByRole("button", { name: /send/i }));

    await user.click(await screen.findByRole("button", { name: /^retry$/i }));

    expect(await screen.findByText("recovered")).toBeInTheDocument();
    // The question appears once in the transcript, not once per attempt.
    const transcript = screen.getByRole("log", { name: /conversation transcript/i });
    expect(within(transcript).getAllByText("kafka health?")).toHaveLength(1);
  });

  it("explains a rate limit instead of showing a bare error", async () => {
    stubApi(
      async () =>
        new Response(JSON.stringify({ detail: "3 per 1 minute" }), {
          status: 429,
          headers: { "Content-Type": "application/json", "Retry-After": "42" },
        }),
    );
    localStorage.setItem(storageKeys.token, token);
    const user = userEvent.setup();
    render(<App />);

    await user.type(screen.getByRole("textbox", { name: /message/i }), "hello");
    await user.click(screen.getByRole("button", { name: /send/i }));

    expect(await screen.findByText(/too many requests — retry in 42s/i)).toBeInTheDocument();
  });

  it("lets the user stop a turn that is taking too long", async () => {
    // A triage sweep can run for a minute with no partial output; without a
    // stop control the only escape was reloading the page.
    stubApi(
      (input?: unknown) =>
        new Promise<Response>((_resolve, reject) => {
          void input;
          setTimeout(() => reject(new DOMException("aborted", "AbortError")), 50);
        }),
    );
    localStorage.setItem(storageKeys.token, token);
    const user = userEvent.setup();
    render(<App />);

    await user.type(screen.getByRole("textbox", { name: /message/i }), "run triage");
    await user.click(screen.getByRole("button", { name: /send/i }));

    const stop = await screen.findByRole("button", { name: /stop/i });
    await user.click(stop);

    expect(await screen.findByText(/stopped\./i)).toBeInTheDocument();
  });
});
