import { afterEach, describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import { storageKeys } from "./config";
import { makeToken, jsonResponse, stubApi, restoreGlobals } from "./test/appHarness";

// Resilience and safety posture: autonomy, the environment check, retries and limits.

afterEach(restoreGlobals);

describe("resilience and safety posture", () => {
  const token = makeToken({ sub: "alice", roles: ["operator"] });

  it("offers only the enabled specialists, and says what the autonomy level allows", async () => {
    stubApi(() => Promise.resolve(jsonResponse({ session_id: "s1", response: "ok" })), {
      subject: "alice",
      role: "operator",
      autonomy_level: "L2",
      model_provider: "gemini",
      model_name: "gemini-3-pro",
      self_test_available: true,
      specialists: [
        { key: "kafka", name: "kafka_health_agent" },
        { key: "k8s", name: "k8s_health_agent", label: "Kubernetes" },
      ],
    });
    localStorage.setItem(storageKeys.token, token);
    render(<App />);

    expect(await screen.findByText(/read-only \(autonomy L2\)/)).toHaveTextContent(
      "Ask about Kafka or Kubernetes.",
    );
    expect(screen.getByRole("button", { name: /kafka consumer lag/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /pods in trouble/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /elasticsearch health/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /firing alerts/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /run triage/i })).toHaveAttribute(
      "title",
      "Run a full health sweep across Kafka and Kubernetes",
    );
  });

  it("keeps the stock suggestions for a server that doesn't list specialists", async () => {
    stubApi(() => Promise.resolve(jsonResponse({ session_id: "s1", response: "ok" })));
    localStorage.setItem(storageKeys.token, token);
    render(<App />);

    expect(await screen.findByText(/destructive actions are off here/)).toHaveTextContent(
      "Ask about Kafka, Kubernetes, Elasticsearch, Docker or your alerts.",
    );
    expect(
      screen.getAllByRole("button", { name: /kafka|pods|elasticsearch|alerts/i }),
    ).toHaveLength(4);
  });

  it("shows the server-resolved autonomy level next to the role", async () => {
    stubApi(async () => jsonResponse({ session_id: "s1", response: "hi" }));
    localStorage.setItem(storageKeys.token, token);
    render(<App />);

    // The token says nothing about autonomy — only the server knows it.
    expect(await screen.findByTitle(/autonomy l3/i)).toHaveTextContent("L3");
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
