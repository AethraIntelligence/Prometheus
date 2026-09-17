# ADR 0029: A trace is a sanitized causal read model

## Status

Accepted - 2026-09-17.

## Context

An objective could already be reconstructed from objective, plan, task,
assignment, model-call, tool-call, approval and audit rows, but only by knowing
every table and correlating their identifiers by hand. Live Activity was never
intended to solve that problem: it is an expendable in-memory fan-out for a
watching interface, not a durable record.

Copying all execution state into a second event store would make observability a
second execution engine. Persisting prompts, responses and tool payloads would
also turn a diagnostic feature into a new store of credentials and personal
content.

## Decision

The internal trace schema is version 1. A trace has one `trace_id`; every event
has a stable `span_id`, an optional causal parent, correlation and causation ids,
workspace, actor, entity references, kind, status, reason code, UTC timing and
bounded safe attributes. Objective ids are trace ids for managed requests;
standalone task and workflow-run ids are their own roots. A resumed run keeps
its root and appends a `RESUME` span. Retry, replan, reassignment and escalation
are children and never overwrite the earlier attempt.

Execution repositories remain authoritative. The trace repository projects
their rows into stable spans and stores only causal events that have no other
durable home. This also makes historical rows inspectable without inventing
events that were not recorded. Parallel siblings share a parent and retain UTC
timestamps; sequence is a stable display tie-breaker, not a claim that one
sibling caused another.

Redaction is applied before persistence, rendering and export. It covers
secret-looking field names, bearer values, common key assignments, provider-key
forms, exceptions and explicitly known values. Trace attributes are bounded.
Prompts, responses, file and screenshot contents, raw tool input/output,
environment dumps, database copies and credential material are absent by
default. Missing and redacted content is labelled instead of rendered as empty.

Local tracing needs no collector. Optional export uses a bounded queue that
drops the oldest pending export when full and reports its drop count and last
sanitized error. No exporter failure is allowed to change an execution result.
The OpenTelemetry mapping uses trace/span/parent ids and timing directly,
`gen_ai.operation.name` for the span kind, and preserves internal entity,
reason, effect and approval fields under the `prometheus.*` namespace. Internal
schema versioning remains authoritative when conventions change.

Audit rows form a SHA-256 chain per workspace order. Each hash covers the
canonical sanitized record, sequence and previous hash. A transactional durable
checkpoint stores the current head and count, so changing or deleting the last
row is detectable. Calendar-month `chain_id` labels provide rotation without
breaking continuity. This detects tampering; it cannot prevent a machine owner
from replacing both the local store and the verifier.

Metrics are calculated from the same trace/source records. Every value carries
its count, denominator, window, scope and minimum sample size. Cancellation is
excluded from success, acceptance has its own denominator, p95 requires twenty
samples, zero runs is no data, and unsafe irreversible action without approval
has a zero-count SLO.

## Consequences

Trace Viewer, health, metrics and diagnostic export share one application
boundary. Trace retention removes only trace events, scoped by workspace and
age; it never removes tasks, results or audit evidence. A diagnostic bundle is
previewed before an explicit save and will not overwrite an existing file.

The projection is deliberately more work than serializing a complete runtime
object. It prevents drift, lets old data remain legible and keeps diagnostics
from acquiring authority over execution.
