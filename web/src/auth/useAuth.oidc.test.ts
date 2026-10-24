import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// A UserManager stand-in whose events the tests fire by hand.
type Handlers = {
  renewError: Set<(err: Error) => void>;
  expired: Set<() => void>;
  loaded: Set<(user: { access_token: string }) => void>;
  unloaded: Set<() => void>;
};
const handlers = vi.hoisted<Handlers>(() => ({
  renewError: new Set(),
  expired: new Set(),
  loaded: new Set(),
  unloaded: new Set(),
}));
const removeUser = vi.hoisted(() => vi.fn(async () => {}));
const stored = vi.hoisted(() => ({
  user: { access_token: "live" } as { access_token: string } | null,
}));

vi.mock("../config", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../config")>()),
  isOidcEnabled: true,
}));

vi.mock("./oidc", () => {
  const on = (set: Set<unknown>) => (cb: unknown) => set.add(cb);
  const off = (set: Set<unknown>) => (cb: unknown) => set.delete(cb);
  return {
    hasAuthResponse: () => false,
    completeSignIn: vi.fn(),
    loadUser: async () => stored.user,
    signInRedirect: vi.fn(async () => {}),
    signOutRedirect: vi.fn(async () => {}),
    getUserManager: () => ({
      removeUser,
      events: {
        addUserLoaded: on(handlers.loaded),
        removeUserLoaded: off(handlers.loaded),
        addUserUnloaded: on(handlers.unloaded),
        removeUserUnloaded: off(handlers.unloaded),
        addSilentRenewError: on(handlers.renewError),
        removeSilentRenewError: off(handlers.renewError),
        addAccessTokenExpired: on(handlers.expired),
        removeAccessTokenExpired: off(handlers.expired),
      },
    }),
  };
});

const { useAuth, SESSION_ENDED, EXPIRY_GRACE_MS, isSessionRefusal } = await import("./useAuth");

function refusal(code: string): Error {
  return Object.assign(new Error("Session not active"), { error: code });
}

const fire = {
  renewError: (err: Error) => act(() => handlers.renewError.forEach((cb) => cb(err))),
  expired: () => act(() => handlers.expired.forEach((cb) => cb())),
  loaded: (token: string) =>
    act(() => handlers.loaded.forEach((cb) => cb({ access_token: token }))),
};

async function signedIn() {
  const hook = renderHook(() => useAuth());
  await waitFor(() => expect(hook.result.current.token).toBe("live"));
  vi.useFakeTimers();
  return hook;
}

describe("useAuth when the provider ends the session", () => {
  beforeEach(() => {
    Object.values(handlers).forEach((set: Set<unknown>) => set.clear());
    removeUser.mockClear();
    stored.user = { access_token: "live" };
  });
  afterEach(() => vi.useRealTimers());

  it("signs out with a message, and drops the user, when the refresh is refused", async () => {
    const { result } = await signedIn();

    fire.renewError(refusal("invalid_grant"));

    expect(result.current.token).toBeNull();
    expect(result.current.isAuthenticated).toBe(false);
    expect(result.current.error).toBe(SESSION_ENDED);
    expect(removeUser).toHaveBeenCalled();
  });

  it("keeps the session through a network failure", async () => {
    const { result } = await signedIn();

    fire.renewError(new Error("Failed to fetch"));

    expect(result.current.token).toBe("live");
    expect(result.current.error).toBeNull();
  });

  it("waits for a renew in flight before ending an expired session", async () => {
    const { result } = await signedIn();

    // Waking from sleep: the token expires, then the renew lands.
    fire.expired();
    act(() => vi.advanceTimersByTime(EXPIRY_GRACE_MS / 2));
    expect(result.current.token).toBe("live");
    fire.loaded("renewed");
    act(() => vi.advanceTimersByTime(EXPIRY_GRACE_MS));

    expect(result.current.token).toBe("renewed");
    expect(result.current.error).toBeNull();
  });

  it("ends an expired session that no renew rescued, keeping the stored user", async () => {
    const { result } = await signedIn();

    fire.expired();
    act(() => vi.advanceTimersByTime(EXPIRY_GRACE_MS));

    expect(result.current.token).toBeNull();
    expect(result.current.error).toBe(SESSION_ENDED);
    // A renew still retrying can use its refresh token and bring it back.
    expect(removeUser).not.toHaveBeenCalled();
    fire.loaded("late");
    expect(result.current.token).toBe("late");
    expect(result.current.error).toBeNull();
  });

  it("says nothing when the expired record was never signed in", async () => {
    stored.user = null; // a reload with a stale record: loadUser drops it
    const { result } = renderHook(() => useAuth());
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    vi.useFakeTimers();

    fire.expired();
    act(() => vi.advanceTimersByTime(EXPIRY_GRACE_MS));

    expect(result.current.error).toBeNull();
  });

  it("stops listening when unmounted", async () => {
    const { unmount } = await signedIn();
    unmount();
    Object.values(handlers).forEach((set: Set<unknown>) => expect(set.size).toBe(0));
  });

  it("classifies only OAuth refusals as the session ending", () => {
    expect(isSessionRefusal(refusal("invalid_grant"))).toBe(true);
    expect(isSessionRefusal(refusal("login_required"))).toBe(true);
    expect(isSessionRefusal(refusal("temporarily_unavailable"))).toBe(false);
    expect(isSessionRefusal(new Error("timeout"))).toBe(false);
    expect(isSessionRefusal(null)).toBe(false);
  });
});
