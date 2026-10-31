import { useEffect, useState } from "react";
import { ApiClient, ApiError } from "./api/client";
import { useAuth } from "./auth/useAuth";
import { config } from "./config";
import { AccessDenied } from "./components/AccessDenied";
import { ChatConsole } from "./components/ChatConsole";
import { SignInPanel } from "./components/SignInPanel";
import { TokenGate } from "./components/TokenGate";

type Access = "checking" | "granted" | "denied";

/**
 * On a deployment with `ORRERY_REQUIRED_ROLES`, asks the server once whether this token may use the deployment at all, so a
 * refused person sees one clear page and the console never starts loading.
 * Anything but an `access_denied` answer opens the console, whose own error
 * handling (expired token, server down) is the right place for it.
 */
function useAccess(token: string | null, restricted: boolean = config.accessRestricted): Access {
  const [state, setState] = useState<{ token: string | null; access: Access }>({
    token: null,
    access: "checking",
  });
  useEffect(() => {
    if (token === null || !restricted) return;
    const controller = new AbortController();
    new ApiClient(token)
      .me({ signal: controller.signal })
      .then(() => setState({ token, access: "granted" }))
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        const denied = err instanceof ApiError && err.isAccessDenied;
        setState({ token, access: denied ? "denied" : "granted" });
      });
    return () => controller.abort();
  }, [token, restricted]);
  if (!restricted) return "granted";
  return state.token === token ? state.access : "checking";
}

export function App() {
  const auth = useAuth();
  const access = useAccess(auth.isAuthenticated ? auth.token : null);

  if (!auth.isAuthenticated || auth.token === null) {
    // SSO deployments never show the paste-a-token gate; token deployments
    // never show the SSO button. The mode is deployment configuration, so
    // offering both would only invite the wrong one to be tried.
    if (auth.mode === "oidc") {
      return (
        <SignInPanel onSignIn={auth.signInWithSso} error={auth.error} isLoading={auth.isLoading} />
      );
    }
    const expiredMsg = auth.token !== null ? "That token has expired. Paste a fresh one." : null;
    return <TokenGate onSubmit={auth.signIn} error={auth.error ?? expiredMsg} />;
  }

  if (access === "checking") {
    return (
      <p className="p-6 text-sm text-slate-500 dark:text-slate-400" role="status">
        Checking access…
      </p>
    );
  }
  if (access === "denied") return <AccessDenied onSignOut={auth.signOut} />;

  return <ChatConsole token={auth.token} identity={auth.identity} onSignOut={auth.signOut} />;
}
