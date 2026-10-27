import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { ApprovalCards } from "./ApprovalCard";
import { timeLeft } from "./format";
import type { StudioClient } from "./api";
import type { ApprovalAsk } from "./runState";
import type { PendingApproval } from "./types";

const DIGEST = "a".repeat(64);

const ask: ApprovalAsk = {
  approvalId: "a1",
  nodeId: "review",
  prompt: "Publish these release notes?",
  digest: DIGEST,
  size: 12,
  expiresAt: 1900,
  decision: null,
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
  text: "# Release 0.11",
};

function client(over: Partial<Record<"approvals" | "decide", unknown>> = {}) {
  return {
    approvals: vi.fn().mockResolvedValue({ approvals: [pending] }),
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
    expect(await screen.findByText("# Release 0.11")).toBeInTheDocument();
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
    await screen.findByText("# Release 0.11");
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

  it("renders nothing with no pending ask", () => {
    const { container } = render(
      <ApprovalCards client={client()} runId="run-1" asks={[]} now={1000} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
