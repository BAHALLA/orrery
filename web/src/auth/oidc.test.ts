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

// Set before the manager is built: a tab open across the upgrade to in-memory tokens.
window.sessionStorage.setItem("oidc.user:https://idp/:console", '{"access_token":"secret-old"}');
window.sessionStorage.setItem("orrery.console.keep", "1");

const { completeSignIn, forgetSession, getUserManager, rememberSession, restoreSession } =
  await import("./oidc");

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

describe("a tab open across the upgrade", () => {
  it("loses the tokens an earlier build stored, and nothing else", () => {
    getUserManager();
    expect(window.sessionStorage.getItem("oidc.user:https://idp/:console")).toBeNull();
    expect(window.sessionStorage.getItem("orrery.console.keep")).toBe("1");
  });
});

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

  it("redeems a redirect once when asked twice, and gives both callers its answer", async () => {
    const refused = Object.assign(new Error("login_required"), { error: "login_required" });
    const callback = vi.spyOn(manager(), "signinRedirectCallback").mockRejectedValue(refused);

    const answers = await Promise.allSettled([completeSignIn(), completeSignIn()]);

    expect(callback).toHaveBeenCalledOnce();
    expect(answers.map((a) => a.status)).toEqual(["rejected", "rejected"]);
  });

  it("asks the provider silently after a reload, and only once", async () => {
    rememberSession();
    const redirect = vi.spyOn(manager(), "signinRedirect").mockResolvedValue();

    await restoreSession();
    expect(redirect).toHaveBeenCalledExactlyOnceWith({
      prompt: "none",
      redirectMethod: "replace",
    });

    // Coming back without a session must not redirect again: that would loop.
    await restoreSession();
    expect(redirect).toHaveBeenCalledOnce();
  });
});
