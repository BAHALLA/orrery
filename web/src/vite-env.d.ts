/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string;
  /** OIDC issuer URL. Unset disables SSO and falls back to the token gate. */
  readonly VITE_OIDC_ISSUER?: string;
  readonly VITE_OIDC_CLIENT_ID?: string;
  readonly VITE_OIDC_SCOPE?: string;
  /** Dotted path to the roles claim; must match the server's JWT_ROLE_CLAIM. */
  readonly VITE_OIDC_ROLE_CLAIM?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

/**
 * Set by `/console-config.js` (served by the front door from its
 * `ORRERY_CONSOLE_OIDC_*` environment) before the app loads. Every field is
 * optional; an absent one falls back to the build-time `VITE_*` value.
 */
interface OrreryRuntimeConfig {
  oidc?: { issuer?: string; clientId?: string; scope?: string };
  roleClaim?: string;
  /** `ORRERY_CONSOLE_TITLE` / `_ENVIRONMENT` / `_ACCENT`, validated by the server. */
  branding?: { title?: string; environment?: string; accent?: string };
  /** `ORRERY_CONSOLE_ACCESS_DENIED_URL`: where a person without access is sent (https). */
  accessDeniedUrl?: string;
  /** Set when `ORRERY_REQUIRED_ROLES` is: check `/me` before loading the console. */
  accessRestricted?: boolean;
}

interface Window {
  __ORRERY_CONSOLE_CONFIG__?: OrreryRuntimeConfig;
}
