# ADR 0025: Approvals create exact, revocable capability leases

## Status

Accepted - 2026-09-17.

## Context

An approval used to answer only one pending question. The Ask, Auto and Deny
profiles could change whether a person was interrupted, but there was no safe
way to say "allow this again for this task". Remembering only a tool name would
be too broad: permission to write one report is not permission to write another,
and permission to contact one domain is not permission to contact every domain.

## Decision

Every approval request carries a machine-readable scope beside its human-readable
action: workspace, employee subject, tool action, normalized resource and
call-specific limits. Paths are normalized, URLs are scoped to their domain,
known recipient and object identifiers remain exact, and calls with no natural
resource use a fingerprint of their redacted arguments. Payload-size and numeric
limits may only stay equal or become narrower on later calls.

An explicit approval has three choices:

- `ONCE` releases only the parked call;
- `TASK` creates an exact lease that also requires the same task id;
- `PERSISTENT` creates an exact workspace lease with an optional expiry.

Leases are durable, visible in Settings → Permissions and revocable. Matching
requires the same workspace, subject, action and resource, compatible limits,
an active time window and, for task grants, the same task. A declared `DENY`
policy is evaluated before leases and can never be widened by one.

Ask, Auto and Deny remain request profiles over this gate. Deny refuses before
lease matching, Ask uses a matching exact lease or asks, and Auto resolves each
otherwise-required exact call under the recorded request profile; it does not
change employee tool grants or declared policies.

The approval stores the policy source, scope, preview, chosen grant and lease id.
The audit records both the decision and whether authority came from a policy
threshold or a capability lease. File writes include a bounded unified diff;
other tools include their declared effect, resource and limits.

## Consequences

- A grant for one path, domain, recipient, employee or task cannot authorize an
  adjacent one.
- Permanent authority is never implicit. It requires the persistent choice and
  can be inspected and revoked later.
- Existing terminal and Telegram approvals remain approve-once by default.
- Interfaces carry grant choices; they do not reproduce matching or policy
  logic, which remains in the approval gate and lease repository.
