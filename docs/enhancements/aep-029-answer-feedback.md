# AEP-029: User Feedback on Answers

| Field | Value |
|-------|-------|
| **Status** | <span class="badge badge--amber">proposed</span> |
| **Priority** | <span class="badge badge--blue">P2</span> |
| **Effort** | Medium (3-4 days) |
| **Impact** | Medium-High |
| **Dependencies** | AEP-022 (trajectory capture) — strong; AEP-010 (tracing) — soft |

## Gap Analysis

Quality signal currently arrives as hallway complaints. Nothing links "that
answer was wrong" to the turn that produced it, so a bad answer is re-argued
from memory instead of fixed, and it never becomes a test. The evals
(AEP-002) are hand-written. [AEP-022](aep-022-trajectory-capture.md) proposes
harvesting real runs into evals, but it has no signal for *which* runs are
worth harvesting.

## Proposed Solution

Close the loop in three steps: capture, persist, act.

### Step 1: Capture where people already are

- **Slack / Google Chat:** a 👍/👎 *reaction* on the agent's reply. A 👎 prompts
  one follow-up in the thread: "what was wrong?"
- **Web console:** thumbs buttons under each answer, plus an optional comment.

Two windows, not one:

- the **detail window** (≈ 5 min) decides how long the *next message from that
  person in that thread* is read as the explanation. Keep it short: someone who
  reacts 👎, says nothing, and asks a new question ten minutes later must get an
  answer, not a "thanks, filed";
- the **filing TTL** (≈ 15 min) decides when a 👎 with no explanation is filed
  anyway. An unexplained 👎 is the most common shape of the signal you least
  want to lose, so expiry **files** rather than discards. That is the opposite of
  an unanswered approval card, where expiry must cancel.

The follow-up applies to **one person's** next message, never the whole
thread: in a shared thread, a colleague's unrelated question must reach the
agent.

### Step 2: Persist against the trace, not a transcript

Store `{session_id, invocation_id, trace_id, rating, comment, rater, tools_called,
agent_version}` in an `orrery_feedback` table (Postgres, same `DATABASE_URL`).
The `trace_id` opens the actual run in Tempo (AEP-010). `tools_called` comes from
the `ToolLedgerPlugin` record for that turn. A transcript excerpt is a worse
copy of something the trace already holds.

Run the comment through `PIIRedactionPlugin`'s redaction before storage; people
paste tokens into complaints.

### Step 3: Act

- `orrery_feedback_total{rating,outcome}` (received / filed / timed_out /
  unidentified). Count a reaction whose author cannot be resolved as
  `unidentified`; otherwise negative feedback disappears without a trace.
- A weekly triage view (web console) listing 👎 runs with their trace links.
- **Promotion to an eval:** a reproducible 👎 becomes an AEP-022 harvested
  scenario. That step is what turns feedback into a guarantee guarded by CI.

## Acceptance Criteria

- [ ] Reactions/buttons on Slack, Google Chat and the web console
- [ ] 👎 follow-up bound to the reacting person's next message, short detail window
- [ ] Unexplained 👎 filed at TTL, never dropped
- [ ] Feedback stored with trace id, tools called and agent version; comment redacted
- [ ] `orrery_feedback_total` including `unidentified`
- [ ] One-click promotion of a 👎 run into an AEP-022 eval scenario
