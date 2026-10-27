import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { SpecRefused, type StudioClient } from "./api";
import { describeChanges } from "./format";
import { RegistryDialog } from "./RegistryDialog";
import type { RegistryDocument } from "./types";

const DOCUMENT = {
  a2a: [{ id: "writer", card_url: "https://w.example.com/c", skills: ["draft"] }],
};

function client(doc: Partial<RegistryDocument> = {}, over: Record<string, unknown> = {}) {
  return {
    registryDocument: vi.fn().mockResolvedValue({
      version: 0,
      source: "file",
      editable: true,
      serving_version: 0,
      head_error: null,
      document: DOCUMENT,
      ...doc,
    }),
    saveRegistry: vi.fn().mockResolvedValue({
      version: 1,
      actor: "alice",
      created_at: 1,
      note: "",
      changes: { added: [], removed: [], changed: ["a2a:writer"] },
    }),
    registryHistory: vi.fn().mockResolvedValue([
      {
        version: 1,
        actor: "alice",
        created_at: 1_700_000_000,
        note: "describe the writer",
        changes: { added: [], removed: [], changed: ["a2a:writer"] },
      },
    ]),
    registryVersion: vi.fn().mockResolvedValue({ version: 1, document: { a2a: [] } }),
    ...over,
  } as unknown as StudioClient & Record<string, ReturnType<typeof vi.fn>>;
}

const editor = () => screen.findByLabelText("Registry (JSON)");

describe("describeChanges", () => {
  it("names what changed, or says nothing did", () => {
    expect(describeChanges({ added: ["mcp:docs"], removed: [], changed: ["a2a:w"] })).toBe(
      "added mcp:docs · changed a2a:w",
    );
    expect(describeChanges({ added: [], removed: [], changed: [] })).toBe("no entry changed");
  });
});

describe("RegistryDialog", () => {
  it("saves the edited document with the version it was read at and a note", async () => {
    const c = client();
    const onSaved = vi.fn();
    render(<RegistryDialog client={c} onSaved={onSaved} onClose={() => {}} />);
    const text = await editor();
    fireEvent.change(text, { target: { value: JSON.stringify({ a2a: [] }) } });
    await userEvent.type(screen.getByLabelText(/Note/), "drop the writer");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(c.saveRegistry).toHaveBeenCalledWith({
      document: { a2a: [] },
      version: 0,
      note: "drop the writer",
    });
    expect(await screen.findByText(/Saved as version 1: changed a2a:writer/)).toBeInTheDocument();
    expect(onSaved).toHaveBeenCalled();
  });

  it("shows the server's problems with their paths, and invalid JSON without a request", async () => {
    const c = client(
      {},
      {
        saveRegistry: vi
          .fn()
          .mockRejectedValue(
            new SpecRefused([{ loc: ["document", "mcp", 0], msg: "stdio servers", code: "x" }]),
          ),
      },
    );
    render(<RegistryDialog client={c} onSaved={() => {}} onClose={() => {}} />);
    const text = await editor();
    fireEvent.change(text, { target: { value: "{not json" } });
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/Not valid JSON/)).toBeInTheDocument();
    expect(c.saveRegistry).not.toHaveBeenCalled();
    fireEvent.change(text, { target: { value: "{}" } });
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("mcp → 0: stdio servers")).toBeInTheDocument();
  });

  it("on a conflict, offers the latest version instead of overwriting it", async () => {
    const c = client({}, { saveRegistry: vi.fn().mockRejectedValue(new ApiError(409, "x")) });
    render(<RegistryDialog client={c} onSaved={() => {}} onClose={() => {}} />);
    await editor();
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    fireEvent.change(await editor(), { target: { value: '{"a2a": []}' } });
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await userEvent.click(await screen.findByRole("button", { name: "Reload the latest" }));
    await waitFor(() => expect(c.registryDocument).toHaveBeenCalledTimes(2));
    // The unsaved text survives the reload; Discard shows the latest.
    expect(await editor()).toHaveValue('{"a2a": []}');
    await userEvent.click(screen.getByRole("button", { name: "Discard changes" }));
    expect(await editor()).toHaveValue(JSON.stringify(DOCUMENT, null, 2));
    expect(screen.queryByText(/Someone saved/)).not.toBeInTheDocument();
  });

  it("keeps the confirmation when the saved version is read back", async () => {
    const c = client();
    vi.mocked(c.registryDocument)
      .mockResolvedValueOnce({
        version: 0,
        source: "file",
        editable: true,
        serving_version: 0,
        head_error: null,
        document: DOCUMENT,
      })
      .mockResolvedValue({
        version: 1,
        source: "stored",
        editable: true,
        serving_version: 1,
        head_error: null,
        document: { a2a: [] },
      });
    render(<RegistryDialog client={c} onSaved={() => {}} onClose={() => {}} />);
    fireEvent.change(await editor(), { target: { value: '{"a2a": []}' } });
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(c.registryDocument).toHaveBeenCalledTimes(2));
    expect(await screen.findByText(/Editing version 1/)).toBeInTheDocument();
    expect(screen.getByText(/Saved as version 1/)).toBeInTheDocument();
  });

  it("says when the newest version is not the one that runs", async () => {
    const c = client({ version: 2, source: "stored", serving_version: 1, head_error: "a2a: bad." });
    render(<RegistryDialog client={c} onSaved={() => {}} onClose={() => {}} />);
    expect(
      await screen.findByText(/Version 2 no longer passes the checks, so version 1 runs/),
    ).toBeInTheDocument();
  });

  it("keeps the history listed when one version cannot be read", async () => {
    const c = client(
      { version: 1, source: "stored" },
      { registryVersion: vi.fn().mockRejectedValue(new ApiError(500, "boom")) },
    );
    render(<RegistryDialog client={c} onSaved={() => {}} onClose={() => {}} />);
    await editor();
    await userEvent.click(screen.getByRole("tab", { name: "History" }));
    await userEvent.click(await screen.findByRole("button", { name: "Show version 1" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByText("describe the writer")).toBeInTheDocument();
  });

  it("is read-only, without history, when editing is off", async () => {
    const c = client({ editable: false });
    render(<RegistryDialog client={c} onSaved={() => {}} onClose={() => {}} />);
    expect(await screen.findByLabelText("The registry document")).toHaveTextContent("writer");
    expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "History" })).toBeDisabled();
    // The arrow keys do not land on it either.
    fireEvent.keyDown(screen.getByRole("tablist"), { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: "Document" })).toHaveAttribute("aria-selected", "true");
  });

  it("lists saved versions with who, why and what changed, and shows one", async () => {
    const c = client({ version: 1, source: "stored" });
    render(<RegistryDialog client={c} onSaved={() => {}} onClose={() => {}} />);
    await editor();
    await userEvent.click(screen.getByRole("tab", { name: "History" }));
    expect(await screen.findByText("describe the writer")).toBeInTheDocument();
    expect(screen.getByText("changed a2a:writer")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show version 1" }));
    expect(await screen.findByLabelText("Registry version 1")).toHaveTextContent('"a2a": []');
  });
});
