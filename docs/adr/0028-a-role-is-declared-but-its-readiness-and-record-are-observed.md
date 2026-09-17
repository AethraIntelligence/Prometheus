# ADR 0028: A role is declared, but its readiness and record are observed

## Status

Accepted - 2026-09-17.

## Context

An employee declaration said what a role could do and reach, but every interface
presented that declaration as if it were currently usable. A missing integration,
sandbox tool or suitable model appeared only after delegation failed. The chosen
employee and a sentence were stored on the task, while rejected candidates, the
manager's acceptance verdict and per-assignment outcome were lost. This made a
role difficult to understand and its performance easy to misattribute from the
whole objective.

Repeated successful plans were also rediscovered from scratch. Turning them into
automation without evidence or confirmation, however, would silently promote a
coincidence into autonomous work.

## Decision

**The registry remains the source of role identity.** `employee.yaml` may add a
closed-vocabulary hand-off contract: accepted upstream products, produced
products, required evidence and expected failure kinds. Older declarations load
with explicit permissive defaults and are identified as undeclared. A downstream
task is not started when its employee cannot consume what upstream work actually
delivered.

**Readiness is a live core verdict.** The application assembles facts from the
employee registry, tool registry, integrations, policies and model router. A pure
domain rule returns READY, DEGRADED or UNAVAILABLE with machine reason codes and
recovery locations. Delegation excludes unavailable roles and avoids capabilities
a degraded role has lost. Interfaces render this verdict; they do not reconstruct
it.

**Selection and result judgement are assignment records.** Each assignment keeps
a versioned selection code, explanation, rejected alternatives and candidates
whose declarations do not distinguish them. The task runner stores the acceptance verdict based on evidence.
Metrics use assignments in one workspace and time window, not objectives: outcomes
remain distinct, cancelled and open work do not enter the acceptance denominator,
and every value carries its sample and minimum. Old rows may derive a verdict from
their task record and are labelled as derived.

**A recurring successful structure may become a suggestion.** The fingerprint is
the employee sequence, needs and dependency edges of accepted multi-step plans;
request text and secrets are not copied. Three distinct successful objectives in
one workspace and window create one deterministic, deduplicated suggestion.
Dismissal and snoozing are persisted. Saving needs an explicit confirmation and
writes a manual workflow without running it, scheduling it or adding permissions.

We rejected a separate marketplace-style employee model because it would drift
from executable declarations. We rejected frontend readiness and performance
calculations because different interfaces could disagree. We rejected semantic
similarity of request text and automatic workflow activation because both expand
the privacy and authority boundary without reliable structural evidence.

## Consequences

- Opening Workforce can explain what a role does, whether it can do it here, why
  it received work, what happened and what an accepted result cost.
- Readiness changes when a dependency changes; it is not a cached promise in the
  employee file and does not require a process restart.
- Assignment and suggestion persistence require migration 041. Versioned JSON
  lets older and newer rows fail to “unrecorded” instead of being guessed.
- Small samples deliberately show no rate. This is less visually satisfying than
  an early percentage and more honest.
- Suggestions discover reusable process shape only. A person still owns the
  workflow definition and every later decision to run or schedule it.
