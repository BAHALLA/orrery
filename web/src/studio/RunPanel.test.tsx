import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { StudioClient } from "./api";
import { releaseNotesRun } from "./fixtures/events";
import { applySummary, initialRun, reduceRun, type RunView } from "./runState";
import { RunPanel } from "./RunPanel";
import type { RunSummary, StudioEvent } from "./types";

const NOTE = /isn't kept on this deployment/;

function panel(view: RunView | null) {
  return render(
    <RunPanel
      client={{} as StudioClient}
      view={view}
      input=""
      onInput={vi.fn()}
      focusSignal={0}
      inputIssues={[]}
      problems={0}
      busy={false}
      cancelling={false}
      connection="idle"
      error={null}
      onClearError={vi.fn()}
      onRun={vi.fn()}
      onCancel={vi.fn()}
      onReconnect={vi.fn()}
      now={200}
    />,
  );
}

function fold(events: StudioEvent[]): RunView {
  return events.reduce(reduceRun, initialRun("run-1"));
}

/** The fixture's run, with the content a capturing server adds to its first and last events. */
function captured(input: string, output: string): StudioEvent[] {
  return releaseNotesRun().map((e) =>
    e.type === "run.start"
      ? { ...e, content: input }
      : e.type === "run.end"
        ? { ...e, content: output }
        : e,
  );
}

/** True when *a* comes before *b* in the document. */
function before(a: Element, b: Element): boolean {
  return Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
}

describe("a template's example", () => {
  const props = {
    client: {} as StudioClient,
    view: null,
    focusSignal: 0,
    inputIssues: [],
    problems: 0,
    busy: false,
    cancelling: false,
    connection: "idle" as const,
    error: null,
    onClearError: vi.fn(),
    onRun: vi.fn(),
    onCancel: vi.fn(),
    onReconnect: vi.fn(),
    now: 200,
  };

  it("fills the input when chosen, and never starts a run", () => {
    const onInput = vi.fn();
    const onRun = vi.fn();
    render(
      <RunPanel {...props} input="" onInput={onInput} onRun={onRun} example="Kafka consumer lag" />,
    );
    fireEvent.click(screen.getByRole("button", { name: /use an example/i }));
    expect(onInput).toHaveBeenCalledWith("Kafka consumer lag");
    expect(onRun).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Run input")).toHaveFocus();
  });

  it("is not offered over something already typed", () => {
    render(<RunPanel {...props} input="mine" onInput={vi.fn()} example="Kafka consumer lag" />);
    expect(screen.queryByRole("button", { name: /use an example/i })).not.toBeInTheDocument();
  });
});

describe("RunPanel content", () => {
  it("puts the output first, above budgets, with the input beside it", () => {
    panel(fold(captured("Why the lag?", "The worker is OOM-looping.")));

    const content = screen.getByRole("region", { name: "Content (redacted)" });
    const output = within(content).getByText("The worker is OOM-looping.");
    const input = within(content).getByText("Why the lag?");
    expect(before(output, input)).toBe(true);
    expect(before(content, screen.getByRole("region", { name: "Budgets" }))).toBe(true);
    expect(before(screen.getByText("succeeded"), content)).toBe(true);
    expect(screen.queryByText(NOTE)).not.toBeInTheDocument();
  });

  it("says the text isn't kept when a finished run has none", () => {
    panel(fold(releaseNotesRun()));

    const note = screen.getByText(NOTE);
    expect(note).toHaveTextContent("ORRERY_STUDIO_CAPTURE_CONTENT=true");
    expect(before(note, screen.getByRole("region", { name: "Budgets" }))).toBe(true);
    expect(screen.queryByRole("region", { name: "Content (redacted)" })).not.toBeInTheDocument();
  });

  it("does not say so while the run is going", () => {
    panel(fold(releaseNotesRun().slice(0, 4)));
    expect(screen.queryByText(NOTE)).not.toBeInTheDocument();
  });

  it("does not say so on a run that failed", () => {
    panel(
      fold(
        releaseNotesRun().map((e) =>
          e.type === "run.end" ? { ...e, outcome: "failed", error: "model error" } : e,
        ),
      ),
    );
    expect(screen.getByText("model error")).toBeInTheDocument();
    expect(screen.queryByText(NOTE)).not.toBeInTheDocument();
  });

  it("does not say so when only the summary says how the run ended", () => {
    const summary = { status: "succeeded", dropped_events: 0 } as RunSummary;
    panel(applySummary(initialRun("run-1"), summary));
    expect(screen.getByText("succeeded")).toBeInTheDocument();
    expect(screen.queryByText(NOTE)).not.toBeInTheDocument();
  });

  it("shows the input of a capturing run with no output yet, without the note", () => {
    panel(fold(captured("Why the lag?", "unused").slice(0, 4)));
    expect(screen.getByText("Why the lag?")).toBeInTheDocument();
    expect(screen.queryByText(NOTE)).not.toBeInTheDocument();
  });
});
