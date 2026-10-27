import { StrictMode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import { storageKeys } from "./config";
import { makeToken, jsonResponse, stubApi, restoreGlobals } from "./test/appHarness";

// The admin Agents page: inventory, switches, composed agents and the draft.

afterEach(restoreGlobals);

describe("agents dashboard", () => {
  const token = makeToken({ sub: "alice", roles: ["operator"] });

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
          if (puts.length === 2) {
            // Someone else saved meanwhile: the store is now at version 2.
            version = 2;
            return new Response(
              JSON.stringify({
                detail:
                  "The catalog changed since you loaded it (now version 2). Reload and reapply your edit.",
              }),
              { status: 409 },
            );
          }
          version += 1;
          return jsonResponse(catalogBody());
        }
        if (url.endsWith("/admin/catalog")) return jsonResponse(catalogBody());
        if (url.endsWith("/admin/agents")) {
          // A real round trip: React renders the new catalog before this lands.
          await new Promise((resolve) => setTimeout(resolve, 10));
          return jsonResponse({ agents: [] });
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

    // The reload brings version 2, and the admin's draft survives it: saving
    // again applies it on top of what the other admin saved.
    expect(await screen.findByText("turns off when saved")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Enable k8s" })).toHaveAttribute(
      "aria-checked",
      "false",
    );
    // The admin edits the carried draft further before saving it.
    await user.click(screen.getByRole("switch", { name: "Enable docker" }));
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByText(/saved as version 3/i)).toBeInTheDocument();
    expect(puts[2]).toMatchObject({ version: 2, disabled_specialists: ["k8s", "docker"] });
    // Saved: the failed draft is gone, not re-applied over what was just saved.
    expect(screen.getByRole("switch", { name: "Enable k8s" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.queryByText(/^Unsaved:/)).not.toBeInTheDocument();
  });

  it("shows what is running: versions, model, package and the approval gate", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/admin/agents")) {
          return jsonResponse({
            agents: [
              {
                name: "kafka_health_agent",
                description: "Kafka",
                source: "builtin",
                key: "kafka",
                package: "kafka-health-agent",
                version: "0.8.0",
                model: "gemini-a → gemini-b",
                gated: true,
                tools: [{ name: "reset_offsets", guard: "confirm", reason: "moves offsets" }],
              },
              {
                name: "bare_agent",
                description: "Built by hand",
                source: "plugin",
                gated: false,
                tools: [{ name: "drop_all", guard: "destructive", reason: "" }],
              },
            ],
            deployment: {
              app_name: "orrery",
              orrery_version: "0.8.0",
              adk_version: "2.12.0",
              python_version: "3.14.3",
              model: "gemini-a → gemini-b",
              model_provider: "gemini",
              autonomy_level: "L3",
              admin_editing: false,
              enabled_specialists: "",
              disabled_specialists: "docker",
              started_at: Date.now() / 1000 - 7200,
            },
          });
        }
        if (url.endsWith("/admin/catalog")) return new Response("{}", { status: 404 });
        if (url.endsWith("/me")) {
          return jsonResponse({
            subject: "alice",
            role: "admin",
            autonomy_level: "L3",
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

    const deployment = await screen.findByRole("region", { name: "Deployment" });
    expect(within(deployment).getByText("Orrery").nextSibling).toHaveTextContent("0.8.0");
    expect(within(deployment).getByText("ADK").nextSibling).toHaveTextContent("2.12.0");
    expect(within(deployment).getByText("Up for").nextSibling).toHaveTextContent("2h");
    expect(within(deployment).getByText("Selected by env").nextSibling).toHaveTextContent(
      "not docker",
    );

    const list = screen.getByRole("list", { name: "Agents" });
    expect(within(list).getByText("v0.8.0")).toHaveAttribute("title", "kafka-health-agent 0.8.0");
    expect(within(list).getAllByText("gemini-a → gemini-b")).toHaveLength(1);
    expect(within(list).getByText("approval gate on")).toBeInTheDocument();
    // A hand-built agent holding a destructive tool with no gate is called out.
    expect(within(list).getByText("no approval gate")).toBeInTheDocument();
  });

  it("guides composing an agent: name rules, tool groups, presets and a change summary", async () => {
    const puts: unknown[] = [];
    const catalogBody = {
      editing_enabled: true,
      applied_version: 3,
      catalog: {
        version: 3,
        disabled_specialists: [],
        agents: [
          {
            name: "scaler",
            description: "Scales workloads",
            instruction: "Scale.",
            tools: ["scale_deployment"],
          },
        ],
        updated_by: "bob",
        updated_at: Date.now() / 1000 - 300,
      },
      specialists: [
        {
          key: "k8s",
          agent_name: "k8s_health_agent",
          description: "",
          source: "builtin",
          available: true,
        },
        {
          key: "kafka",
          agent_name: "kafka_health_agent",
          description: "",
          source: "builtin",
          available: true,
        },
      ],
      tools: [
        { ref: "list_pods", specialist: "k8s", guard: "read", reason: "" },
        { ref: "scale_deployment", specialist: "k8s", guard: "confirm", reason: "scales" },
        { ref: "list_topics", specialist: "kafka", guard: "read", reason: "" },
        { ref: "delete_topic", specialist: "kafka", guard: "destructive", reason: "deletes" },
      ],
      file_agents: [],
      reserved_names: ["load_memory"],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/admin/catalog") && init?.method === "PUT") {
          puts.push(JSON.parse(String(init.body)));
          return jsonResponse(catalogBody);
        }
        if (url.endsWith("/admin/catalog")) return jsonResponse(catalogBody);
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
    // As main.tsx renders it: StrictMode runs effect cleanups on mount in dev.
    render(
      <StrictMode>
        <App />
      </StrictMode>,
    );

    await user.click(await screen.findByRole("button", { name: /^agents$/i }));
    expect(
      within(await screen.findByRole("region", { name: "Deployment" })).getByText("Catalog")
        .nextSibling,
    ).toHaveTextContent("v3 · bob · 5m ago");

    // "Compose from these tools" opens the editor narrowed to that specialist.
    const kafkaCard = within(screen.getByRole("list", { name: "Agents" }))
      .getByText("kafka_health_agent")
      .closest("li") as HTMLElement;
    await user.click(within(kafkaCard).getByRole("button", { name: "Compose from these tools" }));
    let form = screen.getByRole("form", { name: "Agent editor" });
    expect(within(form).getByLabelText("Filter tools")).toHaveValue("kafka");
    expect(within(form).queryByRole("checkbox", { name: /list_pods/ })).not.toBeInTheDocument();

    // One click takes a specialist's whole group; the guard summary follows.
    await user.click(within(form).getByRole("checkbox", { name: "All kafka tools" }));
    expect(within(form).getByText(/1 destructive/)).toBeInTheDocument();
    expect(within(form).getByRole("button", { name: "Remove delete_topic" })).toBeInTheDocument();

    // Enter in the filter filters; it doesn't keep a half-written agent.
    await user.type(within(form).getByLabelText("Filter tools"), "{Enter}");
    expect(screen.getByRole("form", { name: "Agent editor" })).toBeInTheDocument();

    // A name the coordinator keeps for itself is refused as typed.
    await user.type(within(form).getByLabelText("Name"), "load_memory");
    await user.tab();
    expect(within(form).getByText(/load_memory is already taken/)).toBeInTheDocument();

    // So is a description under the server's 10-character floor.
    await user.type(within(form).getByLabelText(/^Description/), "Kafka");
    expect(within(form).getByText(/description needs at least 10 characters/)).toBeInTheDocument();

    // A name already in use is refused before anything is queued.
    await user.clear(within(form).getByLabelText("Name"));
    await user.type(within(form).getByLabelText("Name"), "scaler");
    await user.type(within(form).getByLabelText(/^Description/), " helper");
    await user.click(within(form).getByRole("button", { name: "Start from a template" }));
    expect((within(form).getByLabelText("Instruction") as HTMLTextAreaElement).value).toContain(
      "## How to work",
    );
    expect(within(form).getByText(/scaler is already taken/)).toBeInTheDocument();
    expect(within(form).getByRole("button", { name: "Keep agent" })).toBeDisabled();

    // Spaces become underscores as you type, and the rename makes it valid.
    await user.clear(within(form).getByLabelText("Name"));
    await user.type(within(form).getByLabelText("Name"), "topic janitor");
    expect(within(form).getByLabelText("Name")).toHaveValue("topic_janitor");
    await user.click(within(form).getByRole("button", { name: "Remove delete_topic" }));
    await user.click(within(form).getByRole("button", { name: "Keep agent" }));

    // Duplicating an agent opens a copy under a free name.
    await user.click(screen.getByRole("button", { name: "Duplicate agent scaler" }));
    form = screen.getByRole("form", { name: "Agent editor" });
    expect(within(form).getByLabelText("Name")).toHaveValue("scaler_copy");
    expect(within(form).getByLabelText("Name")).toHaveFocus();
    await user.click(within(form).getByRole("button", { name: "Cancel" }));
    // Closing the dialog hands focus back to what opened it.
    expect(screen.getByRole("button", { name: "Duplicate agent scaler" })).toHaveFocus();

    // The save bar says what will change; the filter narrows to what's off.
    await user.click(screen.getByRole("switch", { name: "Enable k8s" }));
    expect(screen.getByText(/turn off k8s · add topic_janitor/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "off" }));
    expect(screen.getByRole("button", { name: "off" })).toHaveAttribute("aria-pressed", "true");
    const list = screen.getByRole("list", { name: "Agents" });
    expect(within(list).getByText("k8s_health_agent")).toBeInTheDocument();
    expect(within(list).queryByText("kafka_health_agent")).not.toBeInTheDocument();
    expect(within(list).getByText("turns off when saved")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(puts[0]).toMatchObject({
      version: 3,
      disabled_specialists: ["k8s"],
      agents: [
        { name: "scaler" },
        { name: "topic_janitor", description: "Kafka helper", tools: ["list_topics"] },
      ],
    });
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
});
