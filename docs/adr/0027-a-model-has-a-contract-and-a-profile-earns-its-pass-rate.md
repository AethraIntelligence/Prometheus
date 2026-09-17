# ADR 0027: A model has a contract, and a routing profile earns its pass rate

## Status

Accepted - 2026-09-17.

## Context

Routing chose a model from capabilities, a quality floor and a configured
default, and that choice was trusted forever. Three things were missing.

Nothing recorded *why* a model ran: the call log had the model, the reason was a
debug line, and model calls were not even attributed to their task. Nothing
changed after a failure the model caused: a task a small model could not do was
retried on the same small model. And the release gate counted every recorded run
whatever models produced it, so replacing a default with a cheaper model kept
the pass rate earned by the model it replaced.

## Decision

**A model has a contract.** `ModelContract` (`domain/llm/catalog.py`) is what
routing may rely on: capabilities, context, a quality tier (FAST, BALANCED,
STRONG), privacy (LOCAL or REMOTE, declared, because a local server may forward
to a cloud), typical latency and cost. `local_only` is a requirement like the
quality floor, and `PROMETHEUS_LOCAL_MODELS_ONLY` applies it to every route.
Latency ranks candidates only where it is known.

**Every call records its routing decision.** The client that routes puts the
decision - task kind, entry, reason, escalation level - in a context variable,
and the meter writes it on the call (migration 040). The task runner bills every
call of a run to its task. The Work Center shows each model per kind of work
with its reason.

**Escalation after a classified failure.** `domain/llm/escalation.py` classifies
by record, never by text: a rejected verification, a planning failure, a spent
step budget, or a result the manager would not accept escalate; a refusal, a
person's no, a cost or time budget, a cancellation or a provider outage do not.
After its normal attempts the supervisor retries such a task once more with the
escalation on its assignment, only if the router says a stronger tier exists.
The router then replaces its normal choice with the cheapest entry of the next
tier and says so in the reason. A person's chosen model is not overruled.

**A profile earns its pass rate.** Each validation run stores the routing
profile it ran under: what the router answers for every kind of work, with a
fingerprint over those routes and every entry's contract. The gate reads only
runs under the current fingerprint. `prometheus validate --baseline` forces all
work onto the strongest text model and records baseline runs; a target's
`max_baseline_drop` limits how far a profile may fall below them. Cost is judged
per successful result (`max_cost_per_success_usd`): a window's total cost over
its passes.

## Consequences

- Changing a default, an entry's model or its quality makes the gate fail with
  missing attempts until the new profile has been measured.
- A baseline must be recorded before a baseline threshold can pass, and it costs
  a run on the most expensive model per attempt.
- Escalation adds at most one attempt per task, only for model-shaped failures,
  and the escalated run is a whole task run - planning, execution and
  verification all step up.
- The profile describes routes for requirement-free work; a component with its
  own floor (the verifiers) may route differently, which the call log shows.
- Routing does not yet choose models from recorded pass rates; evidence gates a
  release rather than steering each call.
