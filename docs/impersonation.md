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
# Kubernetes only: match the API server's --oidc-username-prefix / --oidc-groups-prefix
ORRERY_IMPERSONATE_K8S_USER_PREFIX=oidc:   # default: none
ORRERY_IMPERSONATE_K8S_GROUP_PREFIX=oidc:  # default: none
```

**The user claim must be one users cannot set.** An `email` counts only when
the token says it is verified. Any other claim is trusted as the identity
provider issued it, so don't point `ORRERY_IMPERSONATE_USER_CLAIM` at a claim
people can edit themselves (on many identity providers, `preferred_username`):
whoever renames themselves to a colleague is then impersonated as that
colleague.

**Never a system identity.** Kubernetes reserves `system:` names; impersonating
the group `system:masters` is cluster-admin. A user or group starting with
`system:` (after the prefix) is refused, and the call with it, rather than
forwarded.

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

!!! danger "Impersonating any user or group is cluster-admin"
    The `impersonate` verb on all `users` and `groups` lets the holder become
    anyone, including the group `system:masters`, which bypasses RBAC entirely.
    Orrery refuses to forward `system:` names, but the grant belongs to the
    **service account**: anyone who obtains the pod's token can impersonate
    `system:masters` directly with `kubectl --as-group`. Without
    `resourceNames`, the service account is cluster-admin in all but name.

List the users and groups Orrery may act as. Kubernetes matches
`resourceNames` exactly (no wildcards), so this is the list of people and teams
who use Orrery against this cluster:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: orrery-impersonator
rules:
  - apiGroups: [""]
    resources: ["users"]
    verbs: ["impersonate"]
    resourceNames: ["oidc:alice@example.com", "oidc:bob@example.com"]
  - apiGroups: [""]
    resources: ["groups"]
    verbs: ["impersonate"]
    resourceNames: ["oidc:sre", "oidc:oncall"]
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

The service account's own permissions can then shrink to whatever the platform
itself needs (the unattended sweep runs with them). Each user needs their own
RoleBindings, the ones they'd have with `kubectl`.

The impersonated names must match what your cluster's authenticator calls
people: with OIDC, the same claim as the API server's `--oidc-username-claim`,
and the same prefixes as `--oidc-username-prefix` / `--oidc-groups-prefix`
(set `ORRERY_IMPERSONATE_K8S_USER_PREFIX` / `_GROUP_PREFIX` to them). A prefix
keeps identity-provider names apart from every other kind of user, so a group
created in the identity provider can never collide with one the cluster uses
for something else.

## Elasticsearch setup

```json
PUT _security/role/orrery_run_as
{ "run_as": ["*@example.com"] }
```

`run_as: ["*"]` would include the `elastic` superuser and the built-in system
users. Orrery refuses to act as those, but the grant belongs to the platform's
own Elasticsearch user, so anyone holding its credentials could run as
`elastic` directly. Narrow `run_as` to a pattern only your people match (their
verified email domain) and give the platform's own user that role. Each person's own Elasticsearch roles then
decide what their requests can read.

## What it looks like when it's refused

The model gets the same error a missing permission would produce, worded so it
can tell the user why: *"kubernetes calls act as the requesting user, and this
request has no verified user identity"*. The 403s the target itself returns, for
a user who lacks a permission, reach the model as they always have.
