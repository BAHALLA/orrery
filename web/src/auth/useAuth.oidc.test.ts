import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

// A UserManager stand-in whose events the tests fire by hand.
const handlers = vi.hoisted(() => ({
  renewError: [] as ((err: Error) => void)[],
  expired: [] as (() => void)[],
  loaded: [] as ((user: { access_token: string }) => void)[],
}));
const removeUser = vi.hoisted(() => vi.fn(async () => {}));

vi.mock("../config", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../config")>()),
  isOidcEnabled: true,
}));

vi.mock("./oidc", () => ({
  hasAuthResponse: () => false,
  completeSignIn: vi.fn(),
  loadUser: async () => ({ access_token: "live" }),
  signInRedirect: vi.fn(async () => {}),
  signOutRedirect: vi.fn(async () => {}),
  getUserManager: () => ({
    removeUser,
    events: {
      addUserLoaded: (cb: (user: { access_token: string }) => void) => handlers.loaded.push(cb),
      removeUserLoaded: () => {},
      addUserUnloaded: () => {},
      removeUserUnloaded: () => {},
      addSilentRenewError: (cb: (err: Error) => void) => handlers.renewError.push(cb),
      removeSilentRenewError: () => {},
      addAccessTokenExpired: (cb: () => void) => handlers.expired.push(cb),
      removeAccessTokenExpired: () => {},
    },
  }),
}));

const { useAuth, SESSION_ENDED, isSessionRefusal } = await import("./useAuth");

function refusal(code: string): Error {
  return Object.assign(new Error("Session not active"), { error: code });
}

async function signedIn() {
  const hook = renderHook(() => useAuth());
  await waitFor(() => expect(hook.result.current.token).toBe("live"));
  return hook;
}

describe("useAuth when the provider ends the session", () => {
  beforeEach(() => {
    handlers.renewError.length = 0;
    handlers.expired.length = 0;
    handlers.loaded.length = 0;
    removeUser.mockClear();
  });

  it("signs out with a message when the refresh is refused", async () => {
    const { result } = await signedIn();

    act(() => handlers.renewError.forEach((cb) => cb(refusal("invalid_grant"))));

    expect(result.current.token).toBeNull();
    expect(result.current.isAuthenticated).toBe(false);
    expect(result.current.error).toBe(SESSION_ENDED);
    expect(removeUser).toHaveBeenCalled();
  });

  it("keeps the session through a network failure until the token expires", async () => {
    const { result } = await signedIn();

    act(() => handlers.renewError.forEach((cb) => cb(new Error("Failed to fetch"))));
    expect(result.current.token).toBe("live");

    act(() => handlers.expired.forEach((cb) => cb()));
    expect(result.current.token).toBeNull();
    expect(result.current.error).toBe(SESSION_ENDED);
  });

  it("classifies only OAuth refusals as the session ending", () => {
    expect(isSessionRefusal(refusal("invalid_grant"))).toBe(true);
    expect(isSessionRefusal(refusal("login_required"))).toBe(true);
    expect(isSessionRefusal(refusal("temporarily_unavailable"))).toBe(false);
    expect(isSessionRefusal(new Error("timeout"))).toBe(false);
    expect(isSessionRefusal(null)).toBe(false);
  });
});
