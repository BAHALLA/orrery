import { afterEach, describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import { makeToken, restoreGlobals } from "./test/appHarness";

// Signing in: the token gate, and the console it opens onto.

afterEach(restoreGlobals);

describe("sign-in", () => {
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
});
