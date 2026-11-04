# AEP-033: Studio Replay — A Recorded Run for Anonymous Visitors

| Field | Value |
|-------|-------|
| **Status** | <span class="badge badge--grey">proposed</span> |
| **Priority** | <span class="badge badge--grey">P3</span> |
| **Effort** | Medium (three phases; phase 1 ships alone) |
| **Impact** | Medium (visitors of tirraflow.com; a deployment of Orrery is unchanged) |
| **Dependencies** | [AEP-031](aep-031-orrery-studio.md) (trace events, canvas) |

## Gap Analysis

Studio admits human admins only, and its one public instance sits behind the lab's launch gate and SSO (deploy repo, `docs/labs.md`, ADR-013). Visitors only read about it. A replay plays one real recorded run on the real canvas: nodes lighting up, the A2A exchange, the approval. It needs no LLM, no sign-in and no server.

## Non-goals

- Live runs, editable compositions, typed input, or "what if" branches.
- Any public Orrery endpoint. Nothing in `core/` serves a replay.
- Publishing production or incident runs. Recordings come from demo compositions.
- Automatic publishing: every recording is a deliberate, reviewed act.

## Solution

### 1. The artifact

A versioned JSON file, committed as `web/replay/recordings/<slug>.json`:

```
replay_version, slug, title, recorded_on, spec (filtered), events (filtered),
captions [{seq, text, hold}], expect {outcome, node outcomes, a2a count, approvals}
```

The events alone rebuild the run view. `reduceRun` (`runState.ts:195`) is a pure fold, and saved runs already reopen this way (`SavedRuns.tsx:20`). `RunView` is what `Canvas`, `A2aInspector` and `McpInspector` read. Gaps:

| Gap | Evidence | Decision |
|-----|----------|----------|
| The graph is not in the events; `run.start` lists node ids only | `events.py:81`, `specToGraph` in `graph.ts:99` | The artifact carries the spec |
| The server keeps no spec per run, only composition id and version | `store.py:125` | The exporter reads the stored version, or takes a file; `spec.digest()` must equal `spec_digest` |
| Elapsed times use a wall clock | `useNow.ts`; `now` is a prop on `Canvas` and `RunPanel` | The player passes a virtual clock |
| `startedAt ? … : 0` reads a zero start as unset | `runState.ts:421` | Rebase `ts` to a non-zero origin (this also hides when it ran) |
| The fold only goes forward (`seq <= lastSeq` is skipped) | `runState.ts:196` | A backward scrub refolds from `initialRun`; add keyframes only if measured slow (cap: 2000 events, `events.py:27`) |
| Dropped events leave a hole, such as a node that never exits | `runner.py:192`, `RunEnd.dropped_events` | The exporter refuses such a run |
| `route.decision` and `loop.iteration` reach only the timeline; edge colour comes from node status | `runState.ts:311`, `Canvas.tsx:192` | Accepted for phase 1; captions can name the route |
| `RunPanel` reaches the API client through `ApprovalCard` (its `client` is used only at line 255) | import walk; `ApprovalCard.tsx:2,5` | Split out the read-only part with an approvals slot; `ApprovalCard` stays out |

An import walk confirms `runState`, `graph`, `format`, `Canvas`, `NodeCard`, `A2aInspector` and `McpInspector` have no value import of `api/client`, `api/sse` or `studio/api`. `useStudioRun` does, so the player gets its own small hook. Both inspectors accept `registry: null`. `A2aInspector`'s `onResolve` action must become hideable.

### 2. Producing it safely

**What a run can hold.** Events are names and numbers by default, but `capture()` (`events.py:41`) removes only credentials and emails, plus IPv4 when `ORRERY_REDACT_IPS` is set. A hostname, internal URL or person's name in model text goes through. `prompt`, instructions and descriptions are free text by their authors. `task_id`, `context_id` and `card_digest` identify remote state. The registry names internal hosts (the lab's `demo-writer.studio.internal:8443`). `RunRecord.owner` is the subject, though it is on neither events nor summaries (`store.py:161`). So existing redaction is not a publishing control, and this design does not lean on it.

**The pipeline: an explicit admin action, an allowlist, then a human.**

1. **Export.** `orrery_core/studio/replay_export.py` is a CLI run with the run owner's admin token. It reads existing routes (`GET /studio/runs/{id}`, `/events`, `/studio/compositions/{cid}?version=`), so phase 1 adds no server route, and the server's owner check (`own_run`, `api.py`) applies. Nothing exports on its own.
2. **Allowlist, per event type.** Each output event is built field by field from the table below. Strings outside it never reach the file. An unknown event type or field **refuses the export** until the allowlist is reviewed, so a new `events.py` field fails closed.
3. **Names are declared.** MCP entries, remotes and models must be passed as `--public-name`; any other value refuses. Route labels and node ids must match the spec.
4. **Free text is off.** Each `content`, `prompt`, `description` or `error` is dropped unless named by `--keep-text <json path>`. A node's `instruction` is never published: `--keep-text` refuses it, and `replay_check` fails on a committed file that holds one.
5. **Scan what is kept.** `redact_text` with emails and IPs forced on, plus checks for URLs, hostnames and an operator's private `--deny-file`. A hit refuses rather than rewrites, because a rewrite would differ from what was reviewed. `--allow-host` admits public hosts such as `tirraflow.com`.
6. **Review.** The CLI writes the file and prints every string in it with its JSON path, its class (name or kept text) and the counts dropped. The author reads this **before pushing**: git history is publication, on the assumption that the repository stays public.
7. **CI re-checks.** A `replay_check` test runs steps 2 to 5 on committed files, so a hand edit cannot add a field. The pull request diff is the second reader.

| Source | Published | Dropped or replaced |
|--------|-----------|---------------------|
| every event | `type`, `seq`, rebased `ts` | `run_id` becomes the slug |
| `run.start` | `spec_name`, `pattern`, `nodes`, `budgets` | `spec_digest` emptied; composition id and version; `content` unless kept |
| `node.enter`, `node.exit` | `node_id`, `kind`, `model`, `parent`, `tokens`, `elapsed_ms`, `outcome` | `content` unless kept |
| `route.decision`, `loop.iteration`, `budget.hit` | all fields, strings matched to the spec | none |
| `mcp.call` | all fields, `entry` and `tool` declared | none |
| `a2a.message` | `node_id`, `remote`, `skill`, `direction`, `method`, `task_state`, `states`, `parts`, `elapsed_ms`, `status` | `task_id`, `context_id`, `card_digest`; `content` unless kept |
| `approval.requested` | `node_id`, `kind`, `remote`, `size`, rebased `expires_at` | `approval_id` renumbered; `digest` emptied (it confirms guesses at short text); `prompt` made generic and `content` dropped unless kept |
| `approval.decided` | `node_id`, `kind`, `decision`, `waited_ms` | `approval_id` renumbered; the reviewer's comment, always |
| `run.end` | `outcome`, `totals`, `dropped_events` (always 0) | `error`, `content` unless kept |
| spec | `name`, `pattern`, `budgets`; node `id`, `kind`, `remote`, `tools`, `children`, `model`, output labels; edges | `instruction`, always; `description`, `prompt` unless kept |

**Stays out entirely:** the registry (URLs, trust settings, card status), `RunRecord`, tokens, wall-clock dates beyond the author's `recorded_on`, and the full spec's digest.

**If capture was off.** That is the default (`config.py:44`), and the lab's compose file does not set it. The export still works: canvas, timings, tokens, MCP and A2A structure and the approval's outcome all replay, and only text is absent. Text under review is held in memory and never stored (`api.py` header, `approval.py`), so it cannot be recovered later. The player says "text not recorded" and the captions explain. Past the content budget, events also lose `content` mid-run (`runner.py:192`), so captions must never depend on it.

**Where to record.** Recommended: a local stack with capture on, the demo composition, and an input written for publication. No lab data is nearby. A lab run also resets with the process, since the lab has no database (`labs.md`).

### 3. Serving

**Phase 1: a standalone static bundle at `replay.tirraflow.com`, delivered through the deploy repo. No console route.**

- **Why not a console route.** The lab host answers anonymous visitors with a redirect to the catalog (ADR-013), and the lab is started by hand with Kafka and a model key (`labs.md`). A page for everyone must not depend on either. A console origin also holds a bearer token, possibly an admin's (`security_headers.py:3`), so published text should stay off it.
- **Why not a path on tirraflow.com.** `connect-src 'self'` there would include the site's API. A dedicated host (the origin certificate covers `*.tirraflow.com`) has nothing to call.
- **Separate entry.** `web/replay.html`, `web/replay/main.tsx` and `web/vite.replay.config.ts`, building to `web/dist-replay` with a relative base. It loads no `console-config.js` and no OIDC, and its output is apart from the console's, so it does not ship in the Orrery image.
- **The recording is imported at build time,** so the page makes no network call at all and code and recording cannot disagree.
- **Size.** In a local build, `StudioView` is 342,628 bytes and the React chunk 210,583 (minified, not gzipped). The player drops dialogs, inspector forms, the palette and the API client, but keeps `@xyflow/react`. The saving is not measured (see open questions), so CI gets a size budget from the first real build. The canvas is lazy-loaded so phones never fetch it. Tailwind needs its own entry, since the console keeps one utilities bundle (AEP-031).
- **CSP.** The host sends `build_csp()`'s policy, tightened: `connect-src 'none'`, `frame-src 'none'`, `form-action 'none'`. `script-src` and `style-src` stay `'self'`, and the canvas already runs under them (`web/scripts/studio_smoke.py:21`). `frame-ancestors 'none'` stays for phase 1: articles link rather than embed.
- **It cannot call `/studio/*`,** enforced three ways: (1) a vitest walks value imports from `replay/main.tsx` and fails on `api/client`, `api/sse`, `studio/api`, `config`, `useSystem`, `useStudioRun`, `ApprovalCard`; (2) a build check fails if `dist-replay` contains `/studio/`, `EventSource` or `Bearer`; (3) `connect-src 'none'` at run time, with a smoke test asserting no CSP violation and only static same-origin requests. `make test-web` builds and runs all of it.

### 4. Player

- **Label.** A permanent banner: "Recorded run from `recorded_on`. Nothing is running; no model is called."
- **Transport.** Paused at the start; never autoplays. Play/pause, speed, previous/next event, and a scrubber over events with caption marks. Keyboard operable.
- **Pacing.** Gaps between events are capped so a long model call or human wait does not stall the page. Counters still show recorded times (`elapsed_ms`, `waited_ms`), and the virtual clock follows recorded `ts`.
- **Captions.** Written by the author, keyed to `seq`, in a polite live region. `hold: true` pauses playback until "Continue replay". Captions and kept text render as plain text, never Markdown or links.
- **Approval.** A distinct read-only card: what was asked, then the outcome and time waited at `approval.decided`. No Approve or Reject controls, not even disabled ones. It always holds on "Continue replay", so the moment is seen.
- **Phones.** Below `BREAKPOINT_LG`, where Studio itself stops editing (`StudioView.tsx:865`), no canvas: a vertical list of nodes in run order with status lights, inline A2A and approval cards, and the caption and transport fixed. It reads the same `RunView`.
- **Reduced motion.** `studio.css:104` already handles the animations. The player also starts in step mode and only auto-advances if asked to.

## Risks

| Risk | Handling |
|------|----------|
| **A leak.** Something private is published | Allowlist, default-off text, scan, review, CI re-check. The scan is only a second layer: a project name in kept text needs the reader. If it happens: replace, rebuild, redeploy, rotate anything sensitive; git history stays |
| **Stale recordings.** The event schema changes | `replay_version`, and a pytest validates each committed recording's events with `EventAdapter` (`extra="forbid"`), so a rename fails in the PR that makes it. A vitest folds each recording and compares it to `expect`. The fix is a small migration or a re-record. Unknown types are already tolerated (`runState.ts:311`), but removals are not, hence the tests. A build-time import rules out version skew at run time |
| **Captions drift** after a re-record | The test fails when a caption's `seq` is absent |
| **It misleads** | A hand-edited file could show events that never ran, and only the PR guards that. The banner says recorded, and the player says when text was left out |
| **Bundle growth** | The size budget above |

## Rollout

1. **Phase 1 (medium, the bulk).** Exporter and `replay_check`; `RunPanel` split; player, phone list, tests; one recording of a composition with an A2A call and an approval. Done when `dist-replay` passes the smoke. Hosting at `replay.tirraflow.com` and delivery of the bundle are a small separate PR in the deploy repo (server block, headers, DNS, routing tests), plus a link from a tirraflow article.
2. **Phase 2 (small to medium).** More recordings and a picker; the review as a console dialog on a saved run, still ending in a file, with a new admin-only route only if the CLI proves too awkward.
3. **Phase 3 (small, optional).** Embedding in articles (`frame-ancestors https://tirraflow.com` on the replay host only; the console's policy never changes) and links to a moment. A console route only if another deployment asks.

`docs/enhancements/README.md` and `mkdocs.yml` get their entries when this is accepted.

## Open questions (not verified)

- How much of the 342,628 bytes is `@xyflow/react`: not measured, so no target size is claimed.
- Whether a read-only `Canvas` (`editable={false}`, no-op handlers) leaves any control that looks editable, such as the lock button (`Canvas.tsx:85`).
- Whether every `RunEnd.error` is free of content: I read two sources (`runner.py:1149`, `:1517`), not all. It is dropped by default for this reason.

- The exact delivery mechanism inside the deploy repo. That repo builds nothing on the VPS (its ADR-003), so the bundle must arrive already built.

## Decisions

Made by the owner:

- **Host:** `replay.tirraflow.com`.
- **Delivery:** through the deploy repo.
- **Text:** the first recording shows some text, from a local capture-on run with an input written for publication.
- **Instructions:** node instructions stay private and are never published.
- **Review:** the CLI is enough as the explicit admin action for phase 1.

Still open:

1. **Recordings in this repository's git history,** or in the deploy repo?
