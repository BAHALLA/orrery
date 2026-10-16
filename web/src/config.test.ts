import { afterEach, describe, expect, it, vi } from "vitest";

// config.ts resolves once at module load, so each case sets the runtime global
// (what /console-config.js does before the app loads) and imports it fresh.
async function loadConfig(runtime?: OrreryRuntimeConfig) {
  vi.resetModules();
  if (runtime === undefined) delete window.__ORRERY_CONSOLE_CONFIG__;
  else window.__ORRERY_CONSOLE_CONFIG__ = runtime;
  return import("./config");
}

afterEach(() => {
  delete window.__ORRERY_CONSOLE_CONFIG__;
  vi.unstubAllEnvs();
});

describe("runtime console config", () => {
  it("without runtime settings, keeps the token gate (no build-time issuer in tests)", async () => {
    const { isOidcEnabled, oidcConfig, config } = await loadConfig();
    expect(isOidcEnabled).toBe(false);
    expect(oidcConfig.clientId).toBe("orrery-console");
    expect(config.roleClaim).toBe("roles");
  });

  it("an empty object (the dev stub) changes nothing", async () => {
    const { isOidcEnabled } = await loadConfig({});
    expect(isOidcEnabled).toBe(false);
  });

  it("turns SSO on from the server's settings, without a rebuild", async () => {
    const { isOidcEnabled, oidcConfig, config } = await loadConfig({
      oidc: { issuer: "https://id.example/realms/lab/", clientId: "orrery-lab" },
      roleClaim: "resource_access.orrery-lab.roles",
    });
    expect(isOidcEnabled).toBe(true);
    expect(oidcConfig.issuer).toBe("https://id.example/realms/lab");
    expect(oidcConfig.clientId).toBe("orrery-lab");
    expect(oidcConfig.scope).toBe("openid profile email");
    expect(config.roleClaim).toBe("resource_access.orrery-lab.roles");
  });

  it("runtime values win over build-time ones; absent ones fall back", async () => {
    vi.stubEnv("VITE_OIDC_ISSUER", "https://build.example/realms/a");
    vi.stubEnv("VITE_OIDC_SCOPE", "openid");
    const { oidcConfig } = await loadConfig({
      oidc: { issuer: "https://runtime.example/realms/b" },
    });
    expect(oidcConfig.issuer).toBe("https://runtime.example/realms/b");
    expect(oidcConfig.scope).toBe("openid");
  });
});
