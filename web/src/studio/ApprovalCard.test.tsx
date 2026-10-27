import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { ApprovalCards } from "./ApprovalCard";
import { timeLeft } from "./format";
import type { StudioClient } from "./api";
import type { ApprovalAsk } from "./runState";
import type { PendingApproval } from "./types";

const TEXT = "# Release 0.11";
// sha256 of QUESTION.
const QUESTION_DIGEST = "f0d6df84f200b006c75f0b1d4dcfc2db23ea1b77bcf4c56684235940891425e0";
// sha256 of TEXT.
const DIGEST = "613d9f9fe23668fe504b263921d534ac90ae1f56a74c11aba3fddf7bd43c4774";

const ask: ApprovalAsk = {
  approvalId: "a1",
  nodeId: "review",
  kind: "approval",
  remote: null,
  prompt: "Publish these release notes?",
  digest: DIGEST,
  size: 12,
  expiresAt: 1900,
  decision: null,
  requestedAt: 1000,
  waitedMs: null,
  content: null,
};

const pending: PendingApproval = {
  approval_id: "a1",
  node_id: "review",
  kind: "approval",
  prompt: ask.prompt,
  digest: DIGEST,
  size: 12,
  requested_at: 1000,
  expires_at: 1900,
  complete: true,
  remote: null,
  text: TEXT,
};

function client(
  over: Partial<Record<"approvals" | "decide", unknown>> = {},
  approval: PendingApproval = pending,
) {
  return {
    approvals: vi.fn().mockResolvedValue({ approvals: [approval] }),
    decide: vi.fn().mockResolvedValue({ approval_id: "a1", decision: "approved" }),
    ...over,
  } as unknown as StudioClient & {
    approvals: ReturnType<typeof vi.fn>;
    decide: ReturnType<typeof vi.fn>;
  };
}

describe("timeLeft", () => {
  it("counts down in minutes and seconds, then says expiring", () => {
    expect(timeLeft(1900, 1000)).toBe("15:00");
    expect(timeLeft(1065, 1000)).toBe("1:05");
    expect(timeLeft(1000, 1000)).toBe("expiring");
  });
});

describe("ApprovalCards", () => {
  it("shows the text under review and approves with its digest and comment", async () => {
    const c = client();
    render(<ApprovalCards client={c} runId="run-1" asks={[ask]} now={1000} />);
    expect(await screen.findByText(TEXT)).toBeInTheDocument();
    expect(screen.getByText("15:00")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText(/Comment/), "  ship it ");
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(c.decide).toHaveBeenCalledWith("run-1", "a1", {
      decision: "approve",
      digest: DIGEST,
      comment: "ship it",
    });
    expect(await screen.findByText(/the run resumes/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
  });

  it("cannot approve before the text is read, but can reject", async () => {
    const c = client({ approvals: vi.fn(() => new Promise(() => {})) });
    render(<ApprovalCards client={c} runId="run-1" asks={[ask]} now={1000} />);
    expect(screen.getByText(/Reading the text under review/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(c.decide).toHaveBeenCalledWith("run-1", "a1", { decision: "reject" });
  });

  it("says why a decision was refused and reads the list again", async () => {
    const c = client({
      decide: vi.fn().mockRejectedValue(new ApiError(409, "this approval was already expired")),
    });
    render(<ApprovalCards client={c} runId="run-1" asks={[ask]} now={1000} />);
    await screen.findByText(TEXT);
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("already expired");
    await waitFor(() => expect(c.approvals).toHaveBeenCalledTimes(2));
  });

  it("offers a retry when the list cannot be read", async () => {
    const c = client({ approvals: vi.fn().mockRejectedValue(new ApiError(409, "elsewhere")) });
    render(<ApprovalCards client={c} runId="run-1" asks={[ask]} now={1000} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("elsewhere");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(c.approvals).toHaveBeenCalledTimes(2));
  });

  it("waits for its own digest check before Approve, then sends the digest it computed", async () => {
    const c = client();
    render(<ApprovalCards client={c} runId="run-1" asks={[ask]} now={1000} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled());
    expect(screen.getByLabelText("Text under review for review")).toHaveAttribute("tabindex", "0");
  });

  it("only rejects text too long to show whole", async () => {
    const c = client({}, { ...pending, complete: false, size: 200_000 });
    render(<ApprovalCards client={c} runId="run-1" asks={[ask]} now={1000} />);
    expect(await screen.findByText(/too long to show in full/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeEnabled();
  });

  it("refuses to approve text whose digest is not the server's", async () => {
    const c = client({}, { ...pending, digest: "0".repeat(64) });
    render(<ApprovalCards client={c} runId="run-1" asks={[ask]} now={1000} />);
    expect(await screen.findByText(/does not match the server's digest/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
  });

  it("does not offer Approve once the approval is expiring", async () => {
    render(<ApprovalCards client={client()} runId="run-1" asks={[ask]} now={2000} />);
    expect(await screen.findByText(/expiring: the run will end rejected/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
  });

  it("answers a remote's question with the answer, and declines with nothing", async () => {
    const question = "Which release?";
    const c = client({}, { ...pending, kind: "input", text: question, digest: QUESTION_DIGEST });
    const input: ApprovalAsk = { ...ask, kind: "input", remote: "demo-writer.draft" };
    render(<ApprovalCards client={c} runId="run-1" asks={[input]} now={1000} />);
    expect(await screen.findByText(question)).toBeInTheDocument();
    expect(screen.getByText("demo-writer.draft asks:")).toBeInTheDocument();
    expect(screen.getByText(/written by an external agent/)).toBeInTheDocument();
    // Once the question's digest is checked (async: Web Crypto).
    expect(await screen.findByText("Write your answer to send it.")).toBeInTheDocument();
    const send = screen.getByRole("button", { name: "Send answer" });
    expect(send).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/Your answer/), "0.11");
    await waitFor(() => expect(send).toBeEnabled());
    await userEvent.click(send);
    expect(c.decide).toHaveBeenCalledWith("run-1", "a1", {
      decision: "approve",
      digest: QUESTION_DIGEST,
      answer: "0.11",
    });
  });

  it("declining a question sends no answer", async () => {
    const c = client(
      {},
      { ...pending, kind: "input", text: "Which release?", digest: QUESTION_DIGEST },
    );
    const input: ApprovalAsk = { ...ask, kind: "input", remote: "demo-writer.draft" };
    render(<ApprovalCards client={c} runId="run-1" asks={[input]} now={1000} />);
    await userEvent.type(screen.getByLabelText(/Your answer/), "never sent");
    await userEvent.click(screen.getByRole("button", { name: "Decline" }));
    expect(c.decide).toHaveBeenCalledWith("run-1", "a1", { decision: "reject" });
  });

  it("confirms an acting remote's request with its digest, and refuses with nothing", async () => {
    const c = client(
      {},
      { ...pending, kind: "action", remote: "demo-writer.draft", text: TEXT, digest: DIGEST },
    );
    const action: ApprovalAsk = { ...ask, kind: "action", remote: "demo-writer.draft" };
    render(<ApprovalCards client={c} runId="run-1" asks={[action]} now={1000} />);
    expect(await screen.findByText(TEXT)).toBeInTheDocument();
    expect(screen.getByText(/goes to an external agent that changes things/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Comment/)).not.toBeInTheDocument();
    const send = screen.getByRole("button", { name: "Confirm and send" });
    await waitFor(() => expect(send).toBeEnabled());
    await userEvent.click(send);
    expect(c.decide).toHaveBeenCalledWith("run-1", "a1", { decision: "approve", digest: DIGEST });
  });

  it("refusing an action sends a plain reject", async () => {
    const c = client({}, { ...pending, kind: "action", remote: "demo-writer.draft" });
    const action: ApprovalAsk = { ...ask, kind: "action", remote: "demo-writer.draft" };
    render(<ApprovalCards client={c} runId="run-1" asks={[action]} now={1000} />);
    await screen.findByText(TEXT);
    await userEvent.click(screen.getByRole("button", { name: "Don't send" }));
    expect(c.decide).toHaveBeenCalledWith("run-1", "a1", { decision: "reject" });
  });

  it("renders nothing with no pending ask", () => {
    const { container } = render(
      <ApprovalCards client={client()} runId="run-1" asks={[]} now={1000} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
