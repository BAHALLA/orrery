# Orrery Web Console

Production web console for the Orrery agent platform — see
[AEP-019](../docs/enhancements/aep-019-web-console.md).

This is an **isolated Node toolchain**, not a member of the Python `uv`
workspace. It builds to a static bundle that the FastAPI front door
(`core/orrery_core/serving/server.py`) serves from `serving/static/` behind the
same JWT auth as the API. `make test` (Python) never touches Node.

Built with **React + Vite + TypeScript** and styled with **Tailwind CSS v4**
(`@tailwindcss/vite`) + `@tailwindcss/typography` for rendered markdown. Dark
mode follows the OS (`prefers-color-scheme`); there is no manual toggle.

The UI is an app shell: a **left sidebar** (brand, New chat, Run triage,
conversation history, identity), a **chat column** in the middle, and a
**right inspector panel** (Tool calls table + Triage report) that toggles from
the header. Conversation history is **server-backed**: `GET /sessions` rebuilds
the sidebar and `GET /session/{id}` fetches a transcript when its conversation
is opened, so history follows the user to any browser. Nothing about a
conversation is written to `localStorage` — only the token mode's bearer token
is stored there.

## Status

Milestone 1 — an authenticated chat console:

- Bearer-token gate (paste the dev JWT from `make run-api`)
- Chat against `POST /chat`, threading the server-issued `session_id`
- Identity + role badge (viewer / operator / admin), decoded client-side for
  display — the server remains authoritative for RBAC
- **Tool calls table** (`GET /session/{id}/activity`): every recorded tool
  execution — time, tool, agent, status — in the right inspector panel, so the
  orchestration is visible instead of an opaque paragraph
- **Confirmation panel** (`GET /confirmations/pending`): when a guarded tool
  is awaiting the caller's decision, an Approve/Deny panel renders inline. The
  buttons send the literal words `approve`/`deny` through the normal chat
  flow — the server's requester-verified gate stays the sole authority
- **Conversation history** sidebar (`GET /sessions`, `GET /session/{id}`,
  `DELETE /session/{id}`) — read from the session store, titled server-side
  from the conversation's first message; New chat starts a fresh session and
  deleting one removes it from the store, not just from this browser
- Loading, error, and auth-expiry states

Milestone 2 — triage view:

- **Run triage** sidebar button — one click sends the full-sweep prompt to the
  `incident_triage_agent`
- **Verdict report** (`GET /session/{id}/triage`): the recorded severity
  (healthy / degraded / critical) as a color-coded badge with the full triage
  report rendered as prose in the inspector's Triage tab, which auto-opens when
  a verdict lands
- **Live tool calls**: the streamed trace (`POST /chat/stream`) shows each
  specialist's calls as they happen; the table, pending approval and triage
  verdict are refreshed once, when the stream reports `done` (or when the
  non-streaming `POST /chat` fallback returns) — no polling

Remaining: per-system status chips, the remediation-loop trace (batch
workflow only today), and Milestone 3 (onboarding wizard).

## Run

From the repo root, wrapped as Make targets (one command each):

```bash
make run-api  # build the SPA + serve it behind the API at http://localhost:8000
make run-web  # Vite dev server on :5173 with hot reload (proxies /chat to :8000)
make install  # npm ci
```

`make run-api` is the production-like path (same-origin, single port). For
fast iteration use two terminals — `make run-api` and `make run-web` —
then mint a token with `make dev-token` and paste it into the gate.

The underlying npm scripts still work directly if you prefer:

```bash
cd web && npm install && npm run dev
```

Copy `.env.example` to `.env.local` to override the API base or dev proxy.

## Quality gate

```bash
make test-web  # lint + format + typecheck + test + build (mirrors CI)
make fmt       # prettier --write .
```

CI runs `make test-web` in a dedicated `web` job (see
`.github/workflows/ci.yml`), so the gate is defined once and matches local.
Underlying scripts: `npm run check`, `npm run test:watch`, `npm run build`.

## Layout

```
src/
  api/           HTTP client + wire types (mirror server.py; will be OpenAPI-generated)
  auth/          token storage, JWT display-decode, role mapping, useAuth
  chat/          useChat controller (drives the active conversation) + message types
  conversations/ client-side history store (useConversations + types)
  components/     app shell — Sidebar, ChatConsole, MessageList, MessageInput,
                  InspectorPanel, ToolCallsTable, TriageReport, ConfirmationPanel,
                  TokenGate, IdentityBadge; severity.ts (shared badge classes)
  styles/         Tailwind entry (@import "tailwindcss") + a little custom CSS
  test/           vitest setup (jsdom shims)
```

## Design notes

- **The console never makes access decisions.** The JWT is decoded client-side
  only to display the subject and role; every request is re-verified server-side
  and RBAC re-derives the role. The confirmation panel is a thin renderer over
  the gate's pending payload — the browser sends the decision word + token, and
  the server decides who may approve (requester-only, args-pinned, TTL'd).
- **Same-origin by default.** `VITE_API_BASE_URL` is empty in production because
  FastAPI serves the bundle itself; a full URL is only for cross-origin dev.
- **Copy follows the deployment.** The empty state's suggestions, the trace's
  agent labels, the triage rollup and the sweep tooltip read the enabled
  specialists from `GET /me` (`specialists: {key, name, label?}[]`) and the
  autonomy level from `autonomy_level`; a server that doesn't send
  `specialists` gets the stock set (`src/specialists.ts`). Display only — the
  server decides what the coordinator can reach.
- **Status colours are tokens.** `ok / warn / crit / guarded / running /
refused` are CSS variables (light and dark) in `styles/index.css`, used via
  `components/severity.ts`. A gate's refusal is `refused` (neutral), not an
  error.
- **Types are hand-maintained for now.** `src/api/types.ts` mirrors the FastAPI
  models; AEP-019 will generate them from the OpenAPI schema so the compiler
  catches contract drift.
