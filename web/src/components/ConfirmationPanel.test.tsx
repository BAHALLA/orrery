import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { ConfirmationPanel } from "./ConfirmationPanel";

const pending = {
  tool_name: "scale_deployment",
  args: { name: "orders", replicas: 0 },
  level: "confirm",
  created_at: 0,
};

describe("approval card", () => {
  it("names the environment on the approve button", () => {
    render(
      <ConfirmationPanel
        pending={pending}
        disabled={false}
        environment="production"
        onDecide={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Approve on production" })).toBeInTheDocument();
    expect(screen.getByText("Approve on PRODUCTION")).toBeInTheDocument();
    expect(screen.getByText("production")).toBeInTheDocument();
  });

  it("keeps the plain label when no environment is configured", () => {
    render(
      <ConfirmationPanel
        pending={pending}
        disabled={false}
        environment={null}
        onDecide={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
  });
});
