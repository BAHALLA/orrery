# Acting as the user (impersonation)

Orrery's RBAC decides whether a tool may run at all. **Impersonation** adds a
second, independent check: the call reaches the target system *as the person who
asked*, so that system's own permissions apply too. A viewer in Orrery who is
also read-only in Kubernetes stays read-only, even through a tool Orrery would
let an operator run. And the Kubernetes audit log names the person, not the bot.

The service account keeps its credentials. It only tells the target on whose
behalf it is calling, and the target authorizes that separately.

| System | How | What the service account needs |
|---|---|---|
| Kubernetes | `Impersonate-User` + one `Impersonate-Group` per group | the `impersonate` verb on `users` and `groups` |
| Elasticsearch | `es-security-runas-user` | a role with `run_as` for the users it may act as |

Kafka and Prometheus have no "act as" mechanism. They need per-user tokens
instead: see [ADR-004](adr/004-user-token-exchange.md).

## Enabling it

```bash
ORRERY_IMPERSONATION=kubernetes,elasticsearch
# How a verified token maps to the identity the target knows (dotted paths):
ORRERY_IMPERSONATE_USER_CLAIM=email        # default: email, falling back to the subject
ORRERY_IMPERSONATE_GROUPS_CLAIM=groups     # default: groups
```

**It fails closed.** With a system enabled, a call to it that has no verified
caller is **refused**, not quietly made as the service account. Two things can
deliberately act as the platform instead:

- the unattended triage sweep (`make run-triage`), which declares it: nobody
  asked, so it runs with the service account's own permissions;
- `ORRERY_IMPERSONATION_FALLBACK=service`, a deployment-wide opt-in for the
  service account whenever no user is known. Use it to roll out gradually. In
  steady state, leave it unset.

## Where the identity comes from

Always from what the transport *verified*. Never from message text, and never
from session state, which tools can write:

| Surface | Identity |
|---|---|
| Web console / HTTP API | the JWT: user from `ORRERY_IMPERSONATE_USER_CLAIM`, groups from `ORRERY_IMPERSONATE_GROUPS_CLAIM`. An `email` counts only with `email_verified: true`; otherwise the subject is used, so a self-asserted address can never borrow a colleague's permissions. Anonymous mode (`AUTH_ENABLED=false`) has none, so impersonating calls are refused. |
| Google Chat | the sender's email from the signed event (on an approval, the original requester's) |
| Slack | the signed event's user id, resolved to an email through `users.info`. Needs the **`users:read.email`** scope. If the lookup fails there is no identity, and calls are refused. |
| CLI (`make run-cli`) | none: set `ORRERY_IMPERSONATION_FALLBACK=service` for local use |

The identity is held in a context variable for the duration of the turn. That
covers every tool call in it, including those inside specialists and worker
threads, and nothing outside it.

## Kubernetes setup

Grant the agent's service account the right to impersonate, and nothing more.
Its own permissions can then shrink to whatever the platform itself needs:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: orrery-impersonator
rules:
  - apiGroups: [""]
    resources: ["users", "groups"]
    verbs: ["impersonate"]
    # Optional: only these identities may be assumed.
    # resourceNames: ["alice@example.com", "sre"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: orrery-impersonator
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: orrery-impersonator}
subjects:
  - kind: ServiceAccount
    name: orrery-assistant
    namespace: orrery
```

Each user then needs their own RoleBindings, the ones they'd have with
`kubectl`. The identity has to match what your cluster's authenticator calls
them: with OIDC, the same claim the API server's `--oidc-username-claim` uses.

## Elasticsearch setup

```json
PUT _security/role/orrery_run_as
{ "run_as": ["*"] }
```

Narrow `run_as` to the users or patterns Orrery may act for, and give the
platform's own user that role. Each person's own Elasticsearch roles then
decide what their requests can read.

## What it looks like when it's refused

The model gets the same error a missing permission would produce, worded so it
can tell the user why: *"kubernetes calls act as the requesting user, and this
request has no verified user identity"*. The 403s the target itself returns, for
a user who lacks a permission, reach the model as they always have.
