import { useAccess } from "./auth/useAccess";
import { useAuth } from "./auth/useAuth";
import { AccessDenied } from "./components/AccessDenied";
import { ChatConsole } from "./components/ChatConsole";
import { SignInPanel } from "./components/SignInPanel";
import { TokenGate } from "./components/TokenGate";

export function App() {
  const auth = useAuth();
  const signedIn = auth.isAuthenticated && auth.token !== null;
  const access = useAccess(
    // Who is signed in, not the token: an SSO silent renew replaces the token
    // and must not re-check (see useAccess).
    signedIn ? (auth.identity?.subject ?? auth.token) : null,
    signedIn ? auth.token : null,
  );

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
