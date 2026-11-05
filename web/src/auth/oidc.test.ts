import { User } from "oidc-client-ts";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../config", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../config")>()),
  isOidcEnabled: true,
  oidcConfig: {
    issuer: "https://idp.example.com/realms/orrery",
    clientId: "orrery-console",
    scope: "openid",
  },
}));

const { forgetSession, getUserManager, rememberSession, restoreSession } = await import("./oidc");

function manager() {
  const um = getUserManager();
  if (!um) throw new Error("OIDC is enabled in this test");
  return um;
}

function signedInUser(): User {
  return new User({
    access_token: "secret-access",
    id_token: "secret-id",
    refresh_token: "secret-refresh",
    token_type: "Bearer",
    profile: { sub: "alice" } as never,
    expires_at: Math.floor(Date.now() / 1000) + 300,
  });
}

function everythingInWebStorage(): string {
  const dump = (s: Storage) =>
    Array.from({ length: s.length }, (_, i) => `${s.key(i)}=${s.getItem(s.key(i) ?? "")}`).join();
  return dump(window.sessionStorage) + dump(window.localStorage);
}

describe("OIDC session", () => {
  beforeEach(async () => {
    window.sessionStorage.clear();
    window.localStorage.clear();
    await manager().removeUser();
    forgetSession();
  });

  afterEach(() => vi.restoreAllMocks());

  it("keeps every token out of web storage", async () => {
    await manager().storeUser(signedInUser());
    rememberSession();

    expect((await manager().getUser())?.access_token).toBe("secret-access");
    expect(everythingInWebStorage()).not.toContain("secret-");
  });

  it("returns the user in memory without leaving the page", async () => {
    await manager().storeUser(signedInUser());
    const redirect = vi.spyOn(manager(), "signinRedirect").mockResolvedValue();

    expect((await restoreSession())?.access_token).toBe("secret-access");
    expect(redirect).not.toHaveBeenCalled();
  });

  it("shows the sign-in panel to a tab that never signed in", async () => {
    const redirect = vi.spyOn(manager(), "signinRedirect").mockResolvedValue();

    expect(await restoreSession()).toBeNull();
    expect(redirect).not.toHaveBeenCalled();
  });

  it("asks the provider silently after a reload, and only once", async () => {
    rememberSession();
    const redirect = vi.spyOn(manager(), "signinRedirect").mockResolvedValue();

    await restoreSession();
    expect(redirect).toHaveBeenCalledExactlyOnceWith({ prompt: "none" });

    // Coming back without a session must not redirect again: that would loop.
    await restoreSession();
    expect(redirect).toHaveBeenCalledOnce();
  });
});
